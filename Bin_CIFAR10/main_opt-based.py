from __future__ import print_function
import sys
import os
pjoin = os.path.join
os.environ["CUDA_VISIBLE_DEVICES"] = sys.argv[sys.argv.index("--gpu") + 1] # The args MUST has an option "--gpu".
import shutil
import time
import argparse
import numpy as np
from PIL import Image
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import glob
# torch
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.serialization import load_lua
import torch.utils.data as Data
import torchvision
import torchvision.utils as vutils
import torchvision.transforms as transforms
from torch.distributions.one_hot_categorical import OneHotCategorical
import torchvision.datasets as datasets
from torch.autograd import Variable
# my libs
from model import AutoEncoders, EMA, Normalize, preprocess_image, recreate_image


def logprint(some_str):
  print(time.strftime("[%Y/%m/%d-%H:%M] ") + str(some_str), file=args.log, flush=True)
  
def path_check(x):
  if x:
    complete_path = glob.glob(x)
    assert(len(complete_path) == 1)
    x = complete_path[0]
  return x

# Passed-in params
parser = argparse.ArgumentParser(description="Knowledge Transfer")
parser.add_argument('--e1',  type=str,   default="models/model_best.pth.tar")
parser.add_argument('--e2',  type=str,   default=None)
parser.add_argument('--pretrained_dir',   type=str, default=None, help="the directory of pretrained decoder models")
parser.add_argument('--pretrained_timeid',type=str, default=None, help="the timeid of the pretrained models.")
parser.add_argument('--num_dec', type=int, default=9)
parser.add_argument('--num_se', type=int, default=1)
parser.add_argument('--t',   type=str,   default=None)
parser.add_argument('--gpu', type=int,   default=0)
parser.add_argument('--lr',  type=float, default=1e-3)
parser.add_argument('--b1',  type=float, default=5e-4, help='adam: decay of first order momentum of gradient')
parser.add_argument('--b2',  type=float, default=5e-4, help='adam: decay of second order momentum of gradient')
# ----------------------------------------------------------------
# various losses
parser.add_argument('--lw_perc', type=float, default=1, help="perceptual loss")
parser.add_argument('--lw_soft', type=float, default=10) # According to the paper KD, the soft target loss weight should be considarably larger than that of hard target loss.
parser.add_argument('--lw_hard', type=float, default=1)
parser.add_argument('--lw_tv',   type=float, default=1e-6)
parser.add_argument('--lw_norm', type=float, default=1e-4)
parser.add_argument('--lw_DA',   type=float, default=10)
parser.add_argument('--lw_adv',  type=float, default=0.5)
parser.add_argument('--lw_class',  type=float, default=10)
# ----------------------------------------------------------------
parser.add_argument('-b', '--batch_size', type=int, default=256)
parser.add_argument('-p', '--project_name', type=str, default="test")
parser.add_argument('-r', '--resume', action='store_true')
parser.add_argument('-m', '--mode', type=str, help='the training mode name.')
parser.add_argument('--num_epoch', type=int, default=96)
parser.add_argument('--debug', action="store_true")
parser.add_argument('--num_class', type=int, default=10)
parser.add_argument('--use_pseudo_code', action="store_false")
parser.add_argument('--begin', type=float, default=25)
parser.add_argument('--end',   type=float, default=20)
parser.add_argument('--temp',  type=float, default=1, help="the tempature in KD")
parser.add_argument('--adv_train', type=int, default=0)
parser.add_argument('--ema_factor', type=float, default=0.9, help="Exponential Moving Average") 
parser.add_argument('--show_interval', type=int, default=10, help="the interval to print logs")
parser.add_argument('--save_interval', type=int, default=100, help="the interval to save sample images")
parser.add_argument('--test_interval', type=int, default=1000, help="the interval to test and save models")
parser.add_argument('--gray', action="store_true")
parser.add_argument('--use_ave_img', action="store_true")
args = parser.parse_args()

# Update and check args
assert(args.num_dec >= 1)
assert(args.mode in AutoEncoders.keys())
args.e1 = path_check(args.e1)
args.e2 = path_check(args.e2)
args.pretrained_dir = path_check(args.pretrained_dir)
args.adv_train = int(args.mode[-1])
args.pid = os.getpid()

# Set up directories and logs, etc.
TIME_ID = time.strftime("%Y%m%d-%H%M")
project_path = pjoin("../Experiments", TIME_ID + "_" + args.project_name)
rec_img_path = pjoin(project_path, "reconstructed_images")
weights_path = pjoin(project_path, "weights") # to save torch model
if not os.path.exists(project_path):
  os.makedirs(project_path)
else:
  if not args.resume:
    shutil.rmtree(project_path)
    os.makedirs(project_path)
if not os.path.exists(rec_img_path):
  os.makedirs(rec_img_path)
if not os.path.exists(weights_path):
  os.makedirs(weights_path)
TIME_ID = "SERVER" + os.environ["SERVER"] + "-" + TIME_ID
log_path = pjoin(weights_path, "log_" + TIME_ID + ".txt")
args.log = sys.stdout if args.debug else open(log_path, "w+")
  
if __name__ == "__main__":
  # Set up model
  AE = AutoEncoders[args.mode]
  ae = AE(args).cuda()
  
  # Set up exponential moving average
  if args.adv_train == 4:
    ema_dec = []; ema_se = []
    for di in range(1, args.num_dec+1):
      ema_dec.append(EMA(args.ema_factor))
      dec = eval("ae.d%s"  % di)
      for name, param in dec.named_parameters():
        if param.requires_grad:
          ema_dec[-1].register(name, param.data)
    for sei in range(1, args.num_se+1):
      ema_se.append(EMA(args.ema_factor))
      se = eval("ae.se%s" % sei)
      for name, param in se.named_parameters():
        if param.requires_grad:
          ema_se[-1].register(name, param.data)
        
  # Prepare data
  # ref: https://github.com/chengyangfu/pytorch-vgg-cifar10/blob/master/main.py
  tensor_normalize = Normalize().cuda()
  normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                    std=[0.229, 0.224, 0.225])
  data_train = datasets.CIFAR10('./CIFAR10_data', train=True, download=True,
                              transform=transforms.Compose([
                                transforms.RandomHorizontalFlip(),
                                transforms.RandomCrop(32, 4),
                                transforms.ToTensor(),
                                normalize,
                              ]))
  if args.gray:
    data_test = datasets.CIFAR10('./CIFAR10_data', train=False, download=True,
                              transform=transforms.Compose([
                                transforms.Grayscale(num_output_channels=3), # test with gray image
                                transforms.ToTensor(),
                                normalize,
                              ]))
  else:
    data_test = datasets.CIFAR10('./CIFAR10_data', train=False, download=True,
                              transform=transforms.Compose([
                                transforms.ToTensor(),
                                normalize,
                              ]))
  kwargs = {'num_workers': 4, 'pin_memory': True}
  train_loader = torch.utils.data.DataLoader(data_train, batch_size=args.batch_size, shuffle=True, **kwargs)
  
  # Prepare transform and one hot generator
  one_hot = OneHotCategorical(torch.Tensor([1./args.num_class] * args.num_class))
  
  # Prepare test code
  # onehot_label = torch.eye(args.num_class)
  # test_codes = torch.randn([args.num_class, args.num_class]) * 5.0 + onehot_label * args.begin
  # test_labels = onehot_label.data.numpy().argmax(axis=1)
  # np.save(pjoin(rec_img_path, "test_codes.npy"), test_codes.data.cpu().numpy())
  
  # Print setting for later check
  logprint(args._get_kwargs())
  
  # Optimizer
  if args.adv_train == 4:
    optimizer_se  = [] 
    optimizer_dec = []
    for di in range(1, args.num_dec+1):
      dec = eval("ae.d"+str(di))
      optimizer_dec.append(torch.optim.Adam(dec.parameters(),  lr=args.lr, betas=(args.b1, args.b2)))
    for sei in range(1, args.num_se+1):
      se = eval("ae.se" + str(sei))
      optimizer_se.append(torch.optim.Adam(se.parameters(),  lr=args.lr, betas=(args.b1, args.b2)))
      
  # Resume previous step
  previous_epoch = previous_step = 0
  if args.e2 and args.resume:
    for clip in os.path.basename(args.e2).split("_"):
      if clip[0] == "E" and "S" in clip:
        num1 = clip.split("E")[1].split("S")[0]
        num2 = clip.split("S")[1]
        if num1.isdigit() and num2.isdigit():
          previous_epoch = int(num1)
          previous_step  = int(num2)
  
  
  # Get the pre-image
  # pre_image = []
  # num_step = 150
  # for cls in range(args.num_class):
    # created_image = np.uint8(np.random.uniform(0, 255, (32, 32, 3)))
    # for i in range(num_step): # 150 steps
      # processed_image = preprocess_image(created_image, False) # normalize
      # optimizer_preimage = torch.optim.SGD([processed_image], lr=6)
      # logits = ae.be(processed_image)
      # class_loss = -logits[0, cls]
      # ae.be.zero_grad()
      # class_loss.backward()
      # optimizer_preimage.step() # optimize the image
      # created_image = recreate_image(processed_image) # 0-1 image to 0-255 image
      # if i == num_step-1:
        # outpath = pjoin(rec_img_path, "%s_preimage_label=%s.jpg" % (TIME_ID, cls))
        # vutils.save_image(processed_image.cpu().data.float(), outpath)
    # pre_image.append(processed_image)
    
    
  # Optimization
  t1 = time.time()
  for epoch in range(previous_epoch, args.num_epoch):
    for step, (img, label) in enumerate(train_loader):
      ae.train()
      # Generate codes randomly
      if args.use_pseudo_code:
        onehot_label = one_hot.sample_n(args.batch_size)
        x = torch.randn([args.batch_size, args.num_class]) * (np.random.rand() * 5.0 + 2.0) + onehot_label * np.random.randint(args.end, args.begin) # logits
        x = x.cuda() / args.temp
        label = onehot_label.data.numpy().argmax(axis=1)
        label = torch.from_numpy(label).long()
      else:
        x = ae.be(img.cuda()) / args.temp
      prob_gt = F.softmax(x, dim=1) # prob, ground truth
      label = label.cuda()
        
      if args.adv_train == 4:
        # update decoder
        imgrec = []; imgrec_DT = []; hardloss_dec = []; trainacc_dec = []; ave_imgrec = Variable(torch.zeros([args.batch_size, 3, 32, 32], requires_grad=True)).cuda()
        for di in range(1, args.num_dec+1):
          dec = eval("ae.d" + str(di)); optimizer = optimizer_dec[di-1]; ema = ema_dec[di-1]
          imgrec1 = dec(x);       feats1 = ae.be.forward_branch(tensor_normalize(imgrec1)); logits1 = feats1[-1]
          imgrec2 = dec(logits1); feats2 = ae.be.forward_branch(tensor_normalize(imgrec2)); # logits2 = feats2[-1]
          imgrec1_DT = ae.defined_trans(imgrec1); logits1_DT = ae.be(tensor_normalize(imgrec1_DT)) # DT: defined transform
          imgrec.append(imgrec1); imgrec_DT.append(imgrec1_DT) # for SE
          ave_imgrec += imgrec1 # to get the average img
          
          tvloss1 = args.lw_tv * (torch.sum(torch.abs(imgrec1[:, :, :, :-1] - imgrec1[:, :, :, 1:])) + 
                                  torch.sum(torch.abs(imgrec1[:, :, :-1, :] - imgrec1[:, :, 1:, :])))
          # tvloss2 = args.lw_tv * (torch.sum(torch.abs(imgrec2[:, :, :, :-1] - imgrec2[:, :, :, 1:])) + 
                                  # torch.sum(torch.abs(imgrec2[:, :, :-1, :] - imgrec2[:, :, 1:, :])))
          imgnorm1 = torch.pow(torch.norm(imgrec1, p=6), 6) * args.lw_norm
          # imgnorm2 = torch.pow(torch.norm(imgrec2, p=6), 6) * args.lw_norm
          
          ploss = 0
          ploss_print = []
          for i in range(len(feats1)-1):
            ploss_print.append(nn.MSELoss()(feats2[i], feats1[i].data) * args.lw_perc)
            ploss += ploss_print[-1]
            
          logprob1 = F.log_softmax(logits1/args.temp, dim=1)
          # logprob2 = F.log_softmax(logits2/args.temp, dim=1)
          # softloss1 = nn.KLDivLoss()(logprob1, prob_gt.data) * (args.temp*args.temp) * args.lw_soft
          # softloss2 = nn.KLDivLoss()(logprob2, prob_gt.data) * (args.temp*args.temp) * args.lw_soft
          hardloss1 = nn.CrossEntropyLoss()(logits1, label.data) * args.lw_hard
          # hardloss2 = nn.CrossEntropyLoss()(logits2, label.data) * args.lw_hard
          hardloss1_DT = nn.CrossEntropyLoss()(logits1_DT, label.data) * args.lw_DA
          
          class_loss = 0
          for i in range(args.batch_size):
            class_loss += -logits1[i, label[i]] * args.lw_class
          
          pred = logits1.detach().max(1)[1]; trainacc = pred.eq(label.view_as(pred)).sum().cpu().data.numpy() / float(args.batch_size)
          hardloss_dec.append(hardloss1.data.cpu().numpy()); trainacc_dec.append(trainacc)
          
          advloss = 0
          for sei in range(1, args.num_se+1):
            se = eval("ae.se" + str(sei))
            logits_dse = se(imgrec1)
            advloss += args.lw_adv / nn.CrossEntropyLoss()(logits_dse, label.data)
          
          ## total loss
          loss = class_loss + \
                  hardloss1 + hardloss1_DT

          dec.zero_grad()
          loss.backward(retain_graph=args.use_ave_img)
        
        # average image loss
        if args.use_ave_img:
          ave_imgrec /= args.num_dec
          logits_ave = ae.be(ave_imgrec)
          hardloss_ave = nn.CrossEntropyLoss()(logits_ave, label.data) * args.lw_hard
          hardloss_ave.backward()
        for di in range(1, args.num_dec+1):
          dec = eval("ae.d" + str(di)); optimizer = optimizer_dec[di-1]; ema = ema_dec[di-1]
          optimizer.step()
          for name, param in dec.named_parameters():
            if param.requires_grad:
              param.data = ema(name, param.data)
        
        # update SE
        hardloss_se = []; trainacc_se = []
        for sei in range(1, args.num_se+1):
          se = eval("ae.se" + str(sei)); optimizer = optimizer_se[sei-1]; ema = ema_se[sei-1]
          se.zero_grad()
          loss_se = 0
          for di in range(args.num_dec):
            logits = se(tensor_normalize(imgrec[di].detach()))
            logits_DT = se(tensor_normalize(imgrec_DT[di].detach()))
            hardloss = nn.CrossEntropyLoss()(logits, label.data) * args.lw_hard
            hardloss_DT = nn.CrossEntropyLoss()(logits_DT, label.data) * args.lw_DA
            loss_se += hardloss + hardloss_DT
            pred = logits.detach().max(1)[1]; trainacc = pred.eq(label.view_as(pred)).sum().cpu().data.numpy() / float(args.batch_size)
            hardloss_se.append(hardloss.data.cpu().numpy()); trainacc_se.append(trainacc)
          loss_se.backward()
          optimizer.step()
          for name, param in se.named_parameters():
            if param.requires_grad:
              param.data = ema(name, param.data)
      
      # Save sample images
      if step % args.save_interval == 0:
        ae.dec = ae.d1
        ae.small_enc = ae.se1
        ae.enc = ae.be
        ae.eval()
        # save some test images
        onehot_label = torch.eye(args.num_class)
        test_codes = torch.randn([args.num_class, args.num_class]) * 5.0 + onehot_label * args.begin
        test_labels = onehot_label.data.numpy().argmax(axis=1)        
        for i in range(len(test_codes)):
          x = test_codes[i].cuda()
          x = x.unsqueeze(0)
          for di in range(1, args.num_dec+1):
            dec = eval("ae.d%s" % di)
            img1 = dec(x)
            out_img1_path = pjoin(rec_img_path, "%s_E%sS%s_imgrec%s_label=%s_d%s.jpg" % (TIME_ID, epoch, step, i, test_labels[i], di))
            vutils.save_image(img1.data.cpu().float(), out_img1_path)
      
      # Test and save models
      if step % args.test_interval == 0:
        ae.dec = ae.d1
        ae.small_enc = ae.se1
        ae.enc = ae.be
        ae.eval()
        # test with the true logits generated from test set
        test_loader = torch.utils.data.DataLoader(data_test, batch_size=100, shuffle=False, **kwargs)
        test_acc = 0
        for i, (img, label) in enumerate(test_loader):
          label = label.cuda()
          # test acc for small enc
          pred = ae.small_enc(img.cuda()).detach().max(1)[1]
          test_acc += pred.eq(label.view_as(pred)).sum().cpu().data.numpy()
        test_acc /= float(len(data_test))
        format_str = "E{}S{} | =======> Test softloss with real logits: test accuracy on SE: {:.4f}"
        logprint(format_str.format(epoch, step, test_acc))
        
        torch.save(ae.se1.state_dict(), pjoin(weights_path, "%s_se_E%sS%s_testacc=%.4f.pth" % (TIME_ID, epoch, step, test_acc)))
        torch.save(ae.d1.state_dict(), pjoin(weights_path, "%s_d1_E%sS%s.pth" % (TIME_ID, epoch, step)))
        for di in range(2, args.num_dec+1):
          dec = eval("ae.d" + str(di))
          torch.save(dec.state_dict(), pjoin(weights_path, "%s_d%s_E%sS%s.pth" % (TIME_ID, di, epoch, step)))

      # Print training loss
      if step % args.show_interval == 0:
        if args.adv_train:
          if args.adv_train in [3, 4]:
            format_str1 = "E{}S{} | dec:"
            format_str2 = " {:.4f}({:.3f})" * args.num_dec
            format_str3 = " | se:"
            format_str4 = " | tv: {:.4f} norm: {:.4f} p:"
            format_str5 = " {:.4f}" * len(ploss_print)
            format_str6 = " ({:.3f}s/step)"
            format_str = "".join([format_str1, format_str2, format_str3, format_str2, format_str4, format_str5, format_str6])
            tmp1 = []; tmp2 = []
            for i in range(args.num_dec):
              tmp1.append(hardloss_dec[i])
              tmp1.append(trainacc_dec[i])
              tmp2.append(hardloss_se[i])
              tmp2.append(trainacc_se[i])
            tmp3 = [x.data.cpu().numpy() for x in ploss_print]
            logprint(format_str.format(epoch, step,
                *tmp1, *tmp2,
                tvloss1.data.cpu().numpy(), imgnorm1.data.cpu().numpy(),
                *tmp3,
                (time.time()-t1)/args.show_interval))

        t1 = time.time()
  log.close()