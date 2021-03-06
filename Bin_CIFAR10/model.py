import torchvision.models as models
import numpy as np
import os
import copy
import torch.nn as nn
import torch
from torch.utils.serialization import load_lua
from torch.distributions.one_hot_categorical import OneHotCategorical
from torchvision import transforms
import torch.nn.functional as F
from torch.autograd import Variable
import math
pjoin = os.path.join

# Exponential Moving Average
class EMA():
  def __init__(self, mu):
    self.mu = mu
    self.shadow = {}
  def register(self, name, val):
    self.shadow[name] = val.clone()
  def __call__(self, name, x):
    assert name in self.shadow
    new_average = (1.0 - self.mu) * x + self.mu * self.shadow[name]
    self.shadow[name] = new_average.clone()
    return new_average

################# CIFAR10 #################
def preprocess_image(pil_im, resize_im=True):
    """
        Processes image for CNNs

    Args:
        PIL_img (PIL_img): Image to process
        resize_im (bool): Resize to 224 or not
    returns:
        im_as_var (torch variable): Variable that contains processed float tensor
    """
    # mean and std list for channels (Imagenet)
    mean = [0.485, 0.456, 0.406]
    std = [0.229, 0.224, 0.225]
    # Resize image
    if resize_im:
        pil_im.thumbnail((512, 512))
    im_as_arr = np.float32(pil_im)
    im_as_arr = im_as_arr.transpose(2, 0, 1)  # Convert array to D,W,H
    # Normalize the channels
    for channel, _ in enumerate(im_as_arr):
        im_as_arr[channel] /= 255
        im_as_arr[channel] -= mean[channel]
        im_as_arr[channel] /= std[channel]
    # Convert to float tensor
    im_as_ten = torch.from_numpy(im_as_arr).float()
    # Add one more channel to the beginning. Tensor shape = 1,3,224,224
    im_as_ten.unsqueeze_(0)
    # Convert to Pytorch variable
    im_as_var = Variable(im_as_ten, requires_grad=True)
    return im_as_var
    
def recreate_image(im_as_var):
    """
        Recreates images from a torch variable, sort of reverse preprocessing
    Args:
        im_as_var (torch variable): Image to recreate
    returns:
        recreated_im (numpy arr): Recreated image in array
    """
    reverse_mean = [-0.485, -0.456, -0.406]
    reverse_std = [1/0.229, 1/0.224, 1/0.225]
    recreated_im = copy.copy(im_as_var.data.numpy()[0])
    for c in range(3):
        recreated_im[c] /= reverse_std[c]
        recreated_im[c] -= reverse_mean[c]
    recreated_im[recreated_im > 1] = 1
    recreated_im[recreated_im < 0] = 0
    recreated_im = np.round(recreated_im * 255)

    recreated_im = np.uint8(recreated_im).transpose(1, 2, 0)
    return recreated_im
# ---------------------------------------------------
cfg = {
    'A': [64, 'M', 128, 'M', 256, 256, 'M', 512, 512, 'M', 512, 512, 'M'],
    'B': [64, 64, 'M', 128, 128, 'M', 256, 256, 'M', 512, 512, 'M', 512, 512, 'M'],
    'D': [64, 64, 'M', 128, 128, 'M', 256, 256, 256, 'M', 512, 512, 512, 'M', 512, 512, 512, 'M'],
    'E': [64, 64, 'M', 128, 128, 'M', 256, 256, 256, 256, 'M', 512, 512, 512, 512, 'M', 512, 512, 512, 512, 'M'],
    'SE': [32, 32, 'M', 64, 64, 'M', 128, 128, 128, 128, 'M', 256, 256, 256, 256, 'M', 256, 256, 256, 256, 'M'],
    'Dec':       ["Up", 512, 512, "Up", 512, 512, "Up", 256, 256, "Up", 128, 128, "Up", 64, 3],
    'Dec_s':     ["Up", 128, 128, "Up", 128, 128, "Up",  64,  64, "Up",  32,  32, "Up", 16, 3],
    'Dec_meta':  ["Up", 128, 128, "Up", 128, 128, "Up",  64,  64, "Up"],
    'Dec_s_aug': ["Up", 128, 128, "Up", 128, 128, "Up",  64,  64, "Up", "64-2", "32x-4", "Up", "16x-x", "3x-x"],
    'Mask':      ["Up",  64,  64, "Up",  64,  64, "Up",  32,  32, "Up",  32,  32, "Up", 16, 1],
    'Dec_gray':  ["Up", 512, 512, "Up", 512, 512, "Up", 256, 256, "Up", 128, 128, "Up", 64, 1],
}

def make_layers(cfg, batch_norm=False):
  layers = []
  in_channels = 3
  for v in cfg:
    if v == 'M':
      layers += [nn.MaxPool2d(kernel_size=2, stride=2)]
    else:
      conv2d = nn.Conv2d(in_channels, v, kernel_size=3, padding=1)
      if batch_norm:
        layers += [conv2d, nn.BatchNorm2d(v), nn.ReLU(inplace=True)]
      else:
        layers += [conv2d, nn.ReLU(inplace=True)]
      in_channels = v
  return nn.Sequential(*layers)
  
def make_layers_dec(cfg, batch_norm=False):
  layers = []
  in_channels = 512
  for v in cfg:
    if v == 'Up':
      layers += [nn.UpsamplingNearest2d(scale_factor=2)]
    else: # conv layer
      if str(v).isdigit():
        v = v
        g = 1
      else:
        g = int(v.split("g")[1])
        v = int(v.split("-")[0])
      conv2d = nn.Conv2d(in_channels, v, kernel_size=3, padding=1, groups=g)
      if batch_norm:
        if v == cfg[-1]:
          layers += [conv2d, nn.BatchNorm2d(v), nn.Sigmoid()] # normalize output image to [0, 1]
        else: 
          layers += [conv2d, nn.BatchNorm2d(v), nn.ReLU(inplace=True)]
      else:
        if v == cfg[-1]:
          layers += [conv2d, nn.Sigmoid()] # normalize output image to [0, 1]
        else: 
          layers += [conv2d, nn.ReLU(inplace=True)]
      in_channels = v
  return nn.Sequential(*layers)
 
def make_layers_augdec(cfg, batch_norm=False, num_divbranch=1):
  layers = []
  in_channels = 512
  for v in cfg:
    if v == 'Up':
      layers += [nn.UpsamplingNearest2d(scale_factor=2)]
    else: # conv layer
      if str(v).isdigit():
        group = 1
      else:
        num_filter, group = v.split("-")
        v = int(num_filter) if num_filter.isdigit() else int(num_filter.split("x")[0]) * num_divbranch
        group = int(group) if group.isdigit() else num_divbranch
      conv2d = nn.Conv2d(in_channels, v, kernel_size=3, padding=1, groups=group)
      if batch_norm:
        if v == cfg[-1]:
          layers += [conv2d, nn.BatchNorm2d(v), nn.Sigmoid()] # normalize output image to [0, 1]
        else: 
          layers += [conv2d, nn.BatchNorm2d(v), nn.ReLU(inplace=True)]
      else:
        if v == cfg[-1]:
          layers += [conv2d, nn.Sigmoid()] # normalize output image to [0, 1]
        else: 
          layers += [conv2d, nn.ReLU(inplace=True)]
      in_channels = v
  return nn.Sequential(*layers)
  
class VGG19(nn.Module):
  def __init__(self, model=None, fixed=None):
    super(VGG19, self).__init__()
    self.features = make_layers(cfg["E"])
    self.features = torch.nn.DataParallel(self.features) # model wrapper that enables parallel GPU utilization
    self.classifier = nn.Sequential(
      nn.Dropout(),
      nn.Linear(512, 512),
      nn.ReLU(True),
      nn.Dropout(),
      nn.Linear(512, 512),
      nn.ReLU(True),
      nn.Linear(512, 10),
    )
    # get layers for forward_branch
    self.branch_layer = ["f0"] # Convx_1. The first layer, i.e., Conv1_1 is included in default.
    self.features_num_module = len(self.features.module)
    
    for i in range(1, self.features_num_module):
      m = self.features.module[i-1]
      if isinstance(m, nn.MaxPool2d):
        self.branch_layer.append("f" + str(i))
      if i == self.features_num_module - 2: # for Huawei's idea
        self.branch_layer.append("f" + str(i))
    
    if model:
     checkpoint = torch.load(model)
     self.load_state_dict(checkpoint["state_dict"])
    else:
      for m in self.modules():
        if isinstance(m, nn.Conv2d):
          n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
          m.weight.data.normal_(0, math.sqrt(2. / n))
          m.bias.data.zero_()
    if fixed:
      for param in self.parameters():
        param.requires_grad = False
    
  def forward(self, x):
    x = self.features(x)
    x = x.view(x.size(0), -1)
    x = self.classifier(x)
    return x
  
  def forward_branch(self, x):
    y = []
    for i in range(self.features_num_module):
      m = self.features.module[i]
      x = m(x)
      if "f" + str(i) in self.branch_layer:
        y.append(x)
    x = x.view(x.size(0), -1)
    x = self.classifier(x)
    y.append(x)
    return y
    
class SmallVGG19(nn.Module):
  def __init__(self, model=None, fixed=None):
    super(SmallVGG19, self).__init__()
    self.features = make_layers(cfg["SE"])
    self.classifier = nn.Sequential(
      nn.Dropout(),
      nn.Linear(256, 512),
      nn.ReLU(True),
      nn.Dropout(),
      nn.Linear(512, 512),
      nn.ReLU(True),
      nn.Linear(512, 10),
    )
    if model:
     checkpoint = torch.load(model)
     self.load_state_dict(checkpoint)
    else:
      for m in self.modules():
        if isinstance(m, nn.Conv2d):
          n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
          m.weight.data.normal_(0, math.sqrt(2. / n))
          m.bias.data.zero_()
    if fixed:
      for param in self.parameters():
          param.requires_grad = False
          
  def forward(self, x):
    x = self.features(x)
    x = x.view(x.size(0), -1)
    x = self.classifier(x)
    return x

class Normalize_CIFAR10(nn.Module):
  def __init__(self):
    super(Normalize_CIFAR10, self).__init__()
    self.normalize = nn.Conv2d(3, 3, kernel_size=(1, 1), stride=(1,1), bias=True, groups=3)
    self.normalize.weight = nn.Parameter(torch.from_numpy(np.array(
                                    [[[[1/0.229]]],
                                     [[[1/0.224]]],
                                     [[[1/0.225]]]])).float()) # 3x1x1x1
    self.normalize.bias = nn.Parameter(torch.from_numpy(np.array(
                                  [-0.485/0.229, -0.456/0.224, -0.406/0.225])).float())
    self.normalize.requires_grad = False
  def forward(self, x):
    return self.normalize(x)
    
class DVGG19(nn.Module):
  def __init__(self, input_dim, model=None, fixed=None, gray=False, num_divbranch=1):
    super(DVGG19, self).__init__()
    self.classifier = nn.Sequential(
      nn.Linear(input_dim, 512),
      nn.ReLU(True),
      nn.Linear(512, 512),
      nn.ReLU(True),
      nn.Linear(512, 512),
      nn.ReLU(True),
    )
    self.gray = gray
    self.features = make_layers_dec(cfg["Dec_gray"]) if gray else make_layers_dec(cfg["Dec_s"], batch_norm=True)

    if model:
     checkpoint = torch.load(model)
     self.load_state_dict(checkpoint)
    else:
      for m in self.modules():
        if isinstance(m, nn.Conv2d):
          n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
          m.weight.data.normal_(0, math.sqrt(2. / n))
          m.bias.data.zero_()

    if fixed:
      for param in self.parameters():
          param.requires_grad = False
          
  def forward(self, x):
    x = self.classifier(x)
    x = x.view(x.size(0), 512, 1, 1)
    x = self.features(x)
    x = torch.stack([x]*3, dim=1).squeeze(2) if self.gray else x
    return x

# mimic the net architecture of MNIST deconv
class DVGG19_deconv(nn.Module):
  def __init__(self, input_dim, model=None, fixed=False, gray=False, num_divbranch=1):
    super(DVGG19_deconv, self).__init__()
    img_size = 32
    num_channel = 3
    self.init_size = img_size // 4
    self.l1 = nn.Sequential(nn.Linear(input_dim, 128 * self.init_size ** 2))
    self.conv_blocks = nn.Sequential(
        nn.BatchNorm2d(128),
        nn.Upsample(scale_factor=2),
        nn.Conv2d(128, 128, 3, stride=1, padding=1),
        nn.BatchNorm2d(128, 0.8),
        nn.LeakyReLU(0.2, inplace=True),
        nn.Upsample(scale_factor=2),
        nn.Conv2d(128, 64, 3, stride=1, padding=1),
        nn.BatchNorm2d(64, 0.8),
        nn.LeakyReLU(0.2, inplace=True),
        nn.Conv2d(64, num_channel, 3, stride=1, padding=1),
        nn.BatchNorm2d(num_channel, 0.8), # Ref: Huawei's paper. They add a BN layer at the end of the generator.
        nn.Tanh(),
    )
  def forward(self, z):
      out = self.l1(z)
      out = out.view(out.shape[0], 128, self.init_size, self.init_size)
      img = self.conv_blocks(out)
      return img
    
class DVGG19_aug(nn.Module): # augmented DVGG19
  def __init__(self, input_dim, model=None, fixed=None, gray=False, num_divbranch=1):
    super(DVGG19_aug, self).__init__()
    self.classifier = nn.Sequential(
      nn.Linear(input_dim, 512),
      nn.ReLU(True),
      nn.Linear(512, 512),
      nn.ReLU(True),
      nn.Linear(512, 512),
      nn.ReLU(True),
    )
    self.gray = gray
    self.features = make_layers_augdec(cfg["Dec_s_aug"], True, num_divbranch)
    self.classifier_num_module = len(self.classifier)
    self.features_num_module = len(self.features)
    self.branch_layer = ["c5", "f3", "f10", "f17", "f24", "f31"]
    
    if model:
     checkpoint = torch.load(model)
     self.load_state_dict(checkpoint)
    else:
      for m in self.modules():
        if isinstance(m, nn.Conv2d):
          n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
          m.weight.data.normal_(0, math.sqrt(2. / n))
          m.bias.data.zero_()

    if fixed:
      for param in self.parameters():
          param.requires_grad = False
          
  def forward(self, x):
    x = self.classifier(x)
    x = x.view(x.size(0), 512, 1, 1)
    x = self.features(x)
    x = torch.stack([x] * 3, dim=1).squeeze(2) if self.gray else x
    return x
    
  def forward_branch(self, x):
    y = []
    for ci in range(self.classifier_num_module):
      m = self.classifier[ci]
      x = m(x)
      if "c" + str(ci) in self.branch_layer:
        y.append(x)
    x = x.view(x.size(0), 512, 1, 1)
    for fi in range(self.features_num_module):
      m = self.features[fi]
      x = m(x)
      if "f" + str(fi) in self.branch_layer:
        y.append(x)
    y.append(x)
    return y
    
# deprecated
class DVGG19_meta(nn.Module):
  def __init__(self, input_dim, model=None, fixed=None, gray=False, num_divbranch=1):
    super(DVGG19_meta, self).__init__()
    self.classifier = nn.Sequential(
      nn.Linear(input_dim, 512),
      nn.ReLU(True),
      nn.Linear(512, 512),
      nn.ReLU(True),
      nn.Linear(512, 512),
      nn.ReLU(True),
    )
    self.features = make_layers_dec(cfg["Dec_meta"], batch_norm=True)
    
    if model:
      checkpoint = torch.load(model)
      self.load_state_dict(checkpoint)
    else:
      for m in self.modules():
        if isinstance(m, nn.Conv2d):
          n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
          m.weight.data.normal_(0, math.sqrt(2. / n))
          m.bias.data.zero_()

    if fixed:
      for param in self.parameters():
          param.requires_grad = False
          
  def forward(self, x):
    x = self.classifier(x)
    x = x.view(x.size(0), 512, 1, 1)
    x = self.features(x)
    return x

# deprecated
class MaskNet(nn.Module):
  def __init__(self, input_dim, model=None, fixed=None, gray=False, num_divbranch=1):
    super(MaskNet, self).__init__()
    self.classifier = nn.Sequential(
      nn.Linear(input_dim, 512),
      nn.ReLU(True),
      nn.Linear(512, 512),
      nn.ReLU(True),
    )
    self.features = make_layers_dec(cfg["Mask"])
    
    if model:
     checkpoint = torch.load(model)
     self.load_state_dict(checkpoint)
    else:
      for m in self.modules():
        if isinstance(m, nn.Conv2d):
          n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
          m.weight.data.normal_(0, math.sqrt(2. / n))
          m.bias.data.zero_()

    if fixed:
      for param in self.parameters():
          param.requires_grad = False
          
  def forward(self, x):
    x = self.classifier(x)
    x = x.view(x.size(0), 512, 1, 1)
    x = self.features(x)
    return x

# deprecated
class MetaNet(nn.Module):
  def __init__(self, input_dim, model=None, fixed=None, gray=False, num_divbranch=1):
    super(MetaNet, self).__init__()
    self.classifier = nn.Sequential(
      nn.Linear(input_dim, 128),
      nn.ReLU(True),
      nn.Linear(128, 128),
      nn.ReLU(True),
      nn.Linear(128, 144),
      nn.ReLU(True),
    ) # 144 = 16x3x3
    self.conv1 = nn.Conv2d(16, 2048, kernel_size=3, padding=1)
    self.conv2 = nn.Conv2d(16, 1024, kernel_size=3, padding=1)
    self.conv3 = nn.Conv2d(16,  512, kernel_size=3, padding=1)
    self.conv4 = nn.Conv2d(16,   48, kernel_size=3, padding=1)
    self.tanh = nn.Tanh()
    
    if model:
     checkpoint = torch.load(model)
     self.load_state_dict(checkpoint)
    else:
      for m in self.modules():
        if isinstance(m, nn.Conv2d):
          n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
          m.weight.data.normal_(0, math.sqrt(2. / n))
          m.bias.data.zero_()
    if fixed:
      for param in self.parameters():
          param.requires_grad = False
          
  def forward(self, x):
    x = self.classifier(x)
    x = x.view(x.size(0), 16, 3, 3)
    y1 = self.tanh(self.conv1(x)) # batch x 2048 x 3 x 3
    y2 = self.tanh(self.conv2(x)) # batch x 1024 x 3 x 3
    y3 = self.tanh(self.conv3(x)) # batch x  512 x 3 x 3
    y4 = self.tanh(self.conv4(x)) # batch x   48 x 3 x 3
    return [y1, y2, y3, y4]

# class DVGG19_deconv(nn.Module):
  # def __init__(self, input_dim, model=None, fixed=None, gray=False, d=128):
    # super(DVGG19_deconv, self).__init__()
    # self.deconv1 = nn.ConvTranspose2d(input_dim, d*4, 4, 1, 0)
    # self.deconv1_bn = nn.BatchNorm2d(d*4)
    # self.relu1 = nn.ReLU(True)
    # self.deconv2 = nn.ConvTranspose2d(d*4, d*2, 4, 2, 1)
    # self.deconv2_bn = nn.BatchNorm2d(d*2)
    # self.relu2 = nn.ReLU(True)
    # self.deconv3 = nn.ConvTranspose2d(d*2, d, 4, 2, 1)
    # self.deconv3_bn =nn.BatchNorm2d(d)
    # self.relu3 = nn.ReLU(True)
    # self.deconv4 = nn.ConvTranspose2d(d, 3, 4, 2, 1)
    # self.tanh = nn.Tanh()
    # self.sigm = nn.Sigmoid()
    
    # # use upsampling for the last conv layer
    # self.upscale = nn.UpsamplingNearest2d(scale_factor=2)
    # self.conv4 = nn.Conv2d(d, 3, 3, 1, 1)
    
    # if model:
     # checkpoint = torch.load(model)
     # self.load_state_dict(checkpoint)
    # else:
      # for m in self.modules():
        # if isinstance(m, nn.Conv2d):
          # n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
          # m.weight.data.normal_(0, math.sqrt(2. / n))
          # m.bias.data.zero_()

    # if fixed:
      # for param in self.parameters():
          # param.requires_grad = False
          
  # def forward(self, x):
    # x = x.view(x.size(0), x.size(1), 1, 1)           # batch x num_z+num_class x 1 x 1
    # x = self.relu1(self.deconv1_bn(self.deconv1(x))) # batch x 512 x 4 x 4
    # x = self.relu2(self.deconv2_bn(self.deconv2(x))) # batch x 256 x 8 x 8
    # x = self.relu3(self.deconv3_bn(self.deconv3(x))) # batch x 128 x 16 x 16
    # x = self.sigm(self.deconv4(x))                   # batch x 3 x 32 x 32
    # return x

################# MNIST #################
class CodeMapping(nn.Module):
  def __init__(self, input_dim, model=None, fixed=False):
    super(CodeMapping, self).__init__()
    self.fc = nn.Sequential(
      nn.Linear(input_dim, 128),
      nn.LeakyReLU(0.2, inplace=True),
      nn.Linear(128, 256),
      nn.LeakyReLU(0.2, inplace=True),
      nn.Linear(256, input_dim),
      nn.LeakyReLU(0.2, inplace=True),
    )
  def forward(self, x):
    return self.fc(x)

# ref: https://github.com/eriklindernoren/PyTorch-GAN/blob/master/implementations/dcgan/dcgan.py
class DLeNet5_deconv(nn.Module):
  def __init__(self, input_dim, model=None, fixed=False, gray=False, num_divbranch=1):
    super(DLeNet5_deconv, self).__init__()
    img_size = 32
    num_channel = 1
    self.init_size = img_size // 4
    self.l1 = nn.Sequential(nn.Linear(input_dim, 128 * self.init_size ** 2))
    self.conv_blocks = nn.Sequential(
        nn.BatchNorm2d(128),
        nn.Upsample(scale_factor=2),
        nn.Conv2d(128, 128, 3, stride=1, padding=1),
        nn.BatchNorm2d(128, 0.8),
        nn.LeakyReLU(0.2, inplace=True),
        nn.Upsample(scale_factor=2),
        nn.Conv2d(128, 64, 3, stride=1, padding=1),
        nn.BatchNorm2d(64, 0.8),
        nn.LeakyReLU(0.2, inplace=True),
        nn.Conv2d(64, num_channel, 3, stride=1, padding=1),
        nn.BatchNorm2d(num_channel, 0.8), # Ref: Huawei's paper. They add a BN layer at the end of the generator.
        nn.Tanh(),
    )
  def forward(self, z):
      out = self.l1(z)
      out = out.view(out.shape[0], 128, self.init_size, self.init_size)
      img = self.conv_blocks(out)
      return img
        
class DLeNet5_upsample(nn.Module):
  def __init__(self, input_dim, model=None, fixed=False, gray=False, num_divbranch=1):
    super(DLeNet5_upsample, self).__init__()
    self.fixed = fixed
    
    self.fc5 = nn.Linear(input_dim, 84)
    self.fc4 = nn.Linear( 84, 120)
    self.fc3 = nn.Linear(120, 400)
    self.conv2 = nn.Conv2d(16, 6, kernel_size=(5, 5), stride=(1, 1), padding=(2, 2)) # to maintain the spatial size, so padding=2
    self.conv1 = nn.Conv2d( 6, 1, kernel_size=(5, 5), stride=(1, 1), padding=(2, 2))
    self.bn2 = nn.BatchNorm2d(6, 0.8)
    self.bn1 = nn.BatchNorm2d(1, 0.8)
    
    self.relu = nn.ReLU(inplace=True)
    self.sigm = nn.Sigmoid()
    self.tanh = nn.Tanh()
    self.relu5 = nn.LeakyReLU(0.2, inplace=True)
    self.relu4 = nn.LeakyReLU(0.2, inplace=True)
    self.relu3 = nn.LeakyReLU(0.2, inplace=True)
    self.relu2 = nn.LeakyReLU(0.2, inplace=True)
    self.relu2 = nn.LeakyReLU(0.2, inplace=True)
    self.unpool = nn.UpsamplingNearest2d(scale_factor=2)
    self.pad = nn.ReflectionPad2d((2,2,2,2))

    if model:
      self.load_state_dict(torch.load(model))
    if fixed:
      for param in self.parameters():
          param.requires_grad = False
      
  def forward(self, y):          # input: 10
    y = self.relu5(self.fc5(y))   # 84
    y = self.relu4(self.fc4(y))   # 120
    y = self.relu3(self.fc3(y))   # 400
    y = y.view(-1, 16, 5, 5)     # 16x5x5
    y = self.unpool(y)           # 16x10x10
    y = self.pad(y)              # 16x14x14
    y = self.relu2(self.bn2(self.conv2(y))) # 6x14x14
    y = self.unpool(y)           # 6x28x28
    y = self.pad(y)              # 6x32x32
    y = self.tanh(self.bn1(self.conv1(y))) # 1x32x32
    return y
    
# Use the LeNet model as https://github.com/iRapha/replayed_distillation/blob/master/models/lenet.py
class LeNet5(nn.Module):
  def __init__(self, model=None, fixed=False):
    super(LeNet5, self).__init__()
    self.fixed = fixed
    
    self.conv1 = nn.Conv2d( 1,  6, kernel_size=(5, 5), stride=(1, 1), padding=(0, 0)); self.pool1 = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)
    self.conv2 = nn.Conv2d( 6, 16, kernel_size=(5, 5), stride=(1, 1), padding=(0, 0)); self.pool2 = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)
    self.fc3 = nn.Linear(400, 120)
    self.fc4 = nn.Linear(120,  84)
    self.fc5 = nn.Linear( 84,  10)
    self.relu = nn.ReLU(inplace=True)
    
    if model:
      self.load_state_dict(torch.load(model))
    if fixed:
      for param in self.parameters():
        param.requires_grad = False
      
  def forward(self, y):          # input: 1x32x32
    y = self.relu(self.conv1(y)) # 6x28x28
    y = self.pool1(y)            # 6x14x14
    y = self.relu(self.conv2(y)) # 16x10x10
    y = self.pool2(y)            # 16x5x5
    y = y.view(y.size(0), -1)    # 400
    y = self.relu(self.fc3(y))   # 120
    y = self.relu(self.fc4(y))   # 84
    y = self.fc5(y)              # 10
    return y
  
  def forward_branch(self, y):
    y = self.relu(self.conv1(y)); out1 = y
    y = self.pool1(y)
    y = self.relu(self.conv2(y)); out2 = y
    y = self.pool2(y)
    y = y.view(y.size(0), -1)
    y = self.relu(self.fc3(y)); out3 = y
    y = self.relu(self.fc4(y)); out4 = y
    y = self.fc5(y)
    return out2, y

# class LeNet5_deep(nn.Module):
  # def __init__(self, model=None, fixed=False):
    # super(LeNet5_deep, self).__init__()
    # self.fixed = fixed
    
    # self.conv1  = nn.Conv2d( 1,  6, kernel_size=(5, 5), stride=(1, 1), padding=(0, 0))
    # self.pool1  = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)
    # self.conv11 = nn.Conv2d( 6,  8, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    # self.conv12 = nn.Conv2d( 8, 10, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    # self.conv13 = nn.Conv2d(10, 12, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    # self.conv14 = nn.Conv2d(12, 14, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    # self.conv2  = nn.Conv2d(14, 16, kernel_size=(5, 5), stride=(1, 1), padding=(0, 0))
    # self.pool2  = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)
    # self.fc3 = nn.Linear(400, 120)
    # self.fc4 = nn.Linear(120,  84)
    # self.fc5 = nn.Linear( 84,  10)
    # self.relu = nn.ReLU(inplace=True)
    
    # if model:
      # self.load_state_dict(torch.load(model))
    # if fixed:
      # for param in self.parameters():
        # param.requires_grad = False
      
  # def forward(self, y):          # input: 1x32x32
    # y = self.relu(self.conv1(y)) # 6x28x28
    # y = self.pool1(y)            # 6x14x14
    # y = self.relu(self.conv11(y))
    # y = self.relu(self.conv12(y))
    # y = self.relu(self.conv13(y))
    # y = self.relu(self.conv14(y))
    # y = self.relu(self.conv2(y)) # 16x10x10
    # y = self.pool2(y)            # 16x5x5
    # y = y.view(y.size(0), -1)    # 400
    # y = self.relu(self.fc3(y))   # 120
    # y = self.relu(self.fc4(y))   # 84
    # y = self.fc5(y)              # 10
    # return y
    
  # def forward_branch(self, y):
    # y = self.relu(self.conv1(y)); out1 = y
    # y = self.pool1(y)
    # y = self.relu(self.conv11(y))
    # y = self.relu(self.conv12(y))
    # y = self.relu(self.conv13(y))
    # y = self.relu(self.conv14(y))
    # y = self.relu(self.conv2(y)); out2 = y
    # y = self.pool2(y)
    # y = y.view(y.size(0), -1)
    # y = self.relu(self.fc3(y)); out3 = y
    # y = self.relu(self.fc4(y)); out4 = y
    # y = self.fc5(y)
    # return out2, y
    
class LeNet5_deep(nn.Module):
  def __init__(self, model=None, fixed=False):
    super(LeNet5_deep, self).__init__()
    self.fixed = fixed
    
    self.conv1  = nn.Conv2d( 1,  6, kernel_size=(5, 5), stride=(1, 1), padding=(0, 0))
    self.pool1  = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)
    self.conv11 = nn.Conv2d( 6,  8, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv12 = nn.Conv2d( 8, 10, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv13 = nn.Conv2d(10, 12, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv14 = nn.Conv2d(12, 14, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv15 = nn.Conv2d(14, 14, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv16 = nn.Conv2d(14, 14, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv17 = nn.Conv2d(14, 14, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv18 = nn.Conv2d(14, 14, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv19 = nn.Conv2d(14, 14, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv110 = nn.Conv2d(14, 14, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv111 = nn.Conv2d(14, 14, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv112 = nn.Conv2d(14, 14, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv113 = nn.Conv2d(14, 14, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv2  = nn.Conv2d(14, 16, kernel_size=(5, 5), stride=(1, 1), padding=(0, 0))
    self.pool2  = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)
    self.fc3 = nn.Linear(400, 120)
    self.fc4 = nn.Linear(120,  84)
    self.fc5 = nn.Linear( 84,  10)
    self.relu = nn.ReLU(inplace=True)
    
    if model:
      self.load_state_dict(torch.load(model))
    if fixed:
      for param in self.parameters():
        param.requires_grad = False
      
  def forward(self, y):          # input: 1x32x32
    y = self.relu(self.conv1(y)) # 6x28x28
    y = self.pool1(y)            # 6x14x14
    y = self.relu(self.conv11(y))
    y = self.relu(self.conv12(y))
    y = self.relu(self.conv13(y))
    y = self.relu(self.conv14(y))
    y = self.relu(self.conv15(y))
    y = self.relu(self.conv16(y))
    y = self.relu(self.conv17(y))
    y = self.relu(self.conv18(y))
    y = self.relu(self.conv19(y))
    # y = self.relu(self.conv110(y))
    # y = self.relu(self.conv111(y))
    # y = self.relu(self.conv112(y))
    # y = self.relu(self.conv113(y))
    y = self.relu(self.conv2(y)) # 16x10x10
    y = self.pool2(y)            # 16x5x5
    y = y.view(y.size(0), -1)    # 400
    y = self.relu(self.fc3(y))   # 120
    y = self.relu(self.fc4(y))   # 84
    y = self.fc5(y)              # 10
    return y
    
  def forward_branch(self, y):
    y = self.relu(self.conv1(y)); out1 = y
    y = self.pool1(y)
    y = self.relu(self.conv11(y))
    y = self.relu(self.conv12(y))
    y = self.relu(self.conv13(y))
    y = self.relu(self.conv14(y))
    y = self.relu(self.conv15(y))
    y = self.relu(self.conv16(y))
    y = self.relu(self.conv17(y))
    y = self.relu(self.conv18(y))
    y = self.relu(self.conv19(y))
    # y = self.relu(self.conv110(y))
    # y = self.relu(self.conv111(y))
    # y = self.relu(self.conv112(y))
    # y = self.relu(self.conv113(y))
    y = self.relu(self.conv2(y)); out2 = y
    y = self.pool2(y)
    y = y.view(y.size(0), -1)
    y = self.relu(self.fc3(y)); out3 = y
    y = self.relu(self.fc4(y)); out4 = y
    y = self.fc5(y)
    return out2, y
    
class SmallLeNet5(nn.Module):
  def __init__(self, model=None, fixed=False):
    super(SmallLeNet5, self).__init__()
    self.fixed = fixed
    
    self.conv1 = nn.Conv2d( 1,  3, kernel_size=(5, 5), stride=(1, 1), padding=(0, 0))
    self.pool1 = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)
    self.conv2 = nn.Conv2d( 3,  8, kernel_size=(5, 5), stride=(1, 1), padding=(0, 0))
    self.pool2 = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)
    self.fc3 = nn.Linear(200, 120)
    self.fc4 = nn.Linear(120,  84)
    self.fc5 = nn.Linear( 84,  10)
    self.relu = nn.ReLU(inplace=True)
    
    if model:
      self.load_state_dict(torch.load(model))
    if fixed:
      for param in self.parameters():
          param.requires_grad = False
      
  def forward(self, y):
    y = self.relu(self.conv1(y))
    y = self.pool1(y)
    y = self.relu(self.conv2(y))
    y = self.pool2(y)
    y = y.view(y.size(0), -1)
    y = self.relu(self.fc3(y))
    y = self.relu(self.fc4(y))
    y = self.fc5(y)
    return y
  
  def forward_branch(self, y):
    y = self.relu(self.conv1(y)); out1 = y
    y = self.pool1(y)
    y = self.relu(self.conv2(y)); out2 = y
    y = self.pool2(y)
    y = y.view(y.size(0), -1)
    y = self.relu(self.fc3(y)); out3 = y
    y = self.relu(self.fc4(y)); out4 = y
    y = self.fc5(y)
    return out1, out2, out3, out4, y    

# class SmallLeNet5_deep(nn.Module):
  # def __init__(self, model=None, fixed=False):
    # super(SmallLeNet5_deep, self).__init__()
    # self.fixed = fixed
    # self.conv1  = nn.Conv2d(1, 3, kernel_size=(5, 5), stride=(1, 1), padding=(0, 0))
    # self.pool1  = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)
    # self.conv11 = nn.Conv2d(3, 4, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    # self.conv12 = nn.Conv2d(4, 5, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    # self.conv13 = nn.Conv2d(5, 6, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    # self.conv14 = nn.Conv2d(6, 7, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    # self.conv2  = nn.Conv2d(7, 8, kernel_size=(5, 5), stride=(1, 1), padding=(0, 0))
    # self.pool2  = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)
    # self.fc3 = nn.Linear(200, 120)
    # self.fc4 = nn.Linear(120,  84)
    # self.fc5 = nn.Linear( 84,  10)
    # self.relu = nn.ReLU(inplace=True)
    
  # def forward(self, y):
    # y = self.relu(self.conv1(y))
    # y = self.pool1(y)
    # y = self.relu(self.conv11(y))
    # y = self.relu(self.conv12(y))
    # y = self.relu(self.conv13(y))
    # y = self.relu(self.conv14(y))
    # y = self.relu(self.conv2(y))
    # y = self.pool2(y)
    # y = y.view(y.size(0), -1)
    # y = self.relu(self.fc3(y))
    # y = self.relu(self.fc4(y))
    # y = self.fc5(y)
    # return y

class SmallLeNet5_deep(nn.Module):
  def __init__(self, model=None, fixed=False):
    super(SmallLeNet5_deep, self).__init__()
    self.fixed = fixed
    self.conv1  = nn.Conv2d(1, 3, kernel_size=(5, 5), stride=(1, 1), padding=(0, 0))
    self.pool1  = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)
    self.conv11 = nn.Conv2d(3, 4, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv12 = nn.Conv2d(4, 5, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv13 = nn.Conv2d(5, 6, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv14 = nn.Conv2d(6, 7, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv15 = nn.Conv2d(7, 7, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv16 = nn.Conv2d(7, 7, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv17 = nn.Conv2d(7, 7, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv18 = nn.Conv2d(7, 7, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv19 = nn.Conv2d(7, 7, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv110 = nn.Conv2d(7, 7, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv111 = nn.Conv2d(7, 7, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv112 = nn.Conv2d(7, 7, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv113 = nn.Conv2d(7, 7, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    self.conv2  = nn.Conv2d(7, 8, kernel_size=(5, 5), stride=(1, 1), padding=(0, 0))
    self.pool2  = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)
    self.fc3 = nn.Linear(200, 120)
    self.fc4 = nn.Linear(120,  84)
    self.fc5 = nn.Linear( 84,  10)
    self.relu = nn.ReLU(inplace=True)
    
  def forward(self, y):
    y = self.relu(self.conv1(y))
    y = self.pool1(y)
    y = self.relu(self.conv11(y))
    y = self.relu(self.conv12(y))
    y = self.relu(self.conv13(y))
    y = self.relu(self.conv14(y))
    y = self.relu(self.conv15(y))
    y = self.relu(self.conv16(y))
    y = self.relu(self.conv17(y))
    y = self.relu(self.conv18(y))
    y = self.relu(self.conv19(y))
    # y = self.relu(self.conv110(y))
    # y = self.relu(self.conv111(y))
    # y = self.relu(self.conv112(y))
    # y = self.relu(self.conv113(y))
    y = self.relu(self.conv2(y))
    y = self.pool2(y)
    y = y.view(y.size(0), -1)
    y = self.relu(self.fc3(y))
    y = self.relu(self.fc4(y))
    y = self.fc5(y)
    return y

class Normalize_MNIST(nn.Module):
  def __init__(self):
    super(Normalize_MNIST, self).__init__()
    self.normalize = nn.Conv2d(1, 1, kernel_size=(1, 1), stride=(1, 1), bias=True, groups=1)
    self.normalize.weight = nn.Parameter(torch.from_numpy(np.array([[[[1/0.3081]]]])).float())
    self.normalize.bias   = nn.Parameter(torch.from_numpy(np.array([-0.1307/0.3081])).float())
    self.normalize.requires_grad = False
  def forward(self, x):
    return self.normalize(x)
    
################# Transform #################
class Transform2(nn.Module): # drop out
  def __init__(self):
    super(Transform2, self).__init__()
    self.drop = nn.Dropout(p=0.08)
  def forward(self, x):
    return self.drop(x)
    
class Transform4(nn.Module): # rand translation
  def __init__(self):
    super(Transform4, self).__init__()
    self.conv_trans = nn.Conv2d(3, 3, kernel_size=(5, 5), stride=(1, 1), padding=(2, 2), bias=False, groups=3)
    self.one_hot2 = OneHotCategorical(torch.Tensor([1/24., 1/24., 1/24., 1/24., 1/24.,
                                                    1/24., 1/24., 1/24., 1/24., 1/24.,
                                                    1/24., 1/24., 0.000, 1/24., 1/24.,
                                                    1/24., 1/24., 1/24., 1/24., 1/24.,
                                                    1/24., 1/24., 1/24., 1/24., 1/24.]))
  def forward(self, x):
    kernel = self.one_hot2.sample().view(1,5,5) # 1x5x5
    kernel = torch.stack([kernel] * 3).cuda() # 3x1x5x5
    self.conv_trans.weight = nn.Parameter(kernel)
    y = self.conv_trans(x)
    self.conv_trans.requires_grad = False
    return y
    
class Transform6(nn.Module): # resize or scale
  def __init__(self):
    super(Transform6, self).__init__()
  def forward(self, x):
    rand_scale = np.random.rand() * 0.05 + 1.03125
    y = F.interpolate(x, scale_factor=rand_scale)
    new_width = int(rand_scale*32)
    w = np.random.randint(new_width-32); h = np.random.randint(new_width-32)
    rand_crop = y[:, :, w:w+32, h:h+32]
    return rand_crop
     
class Transform7(nn.Module): # rotate
  def __init__(self):
    super(Transform7, self).__init__()
    
  def forward(self, x):
    theta = []
    for _ in range(x.shape[0]):
      angle = np.random.randint(-5, 6) / 180.0 * math.pi
      # trans = np.arange(-2, 3) / 32. # 32: the width/height of the MNIST/CIFAR10 image is 32x32
      # trans1 = trans[np.random.randint(len(trans))]
      # trans2 = trans[np.random.randint(len(trans))]
      theta.append([[math.cos(angle), -math.sin(angle), 0],
                    [math.sin(angle),  math.cos(angle), 0]])
    theta = torch.from_numpy(np.array(theta)).float().cuda()
    grid = F.affine_grid(theta, x.size())
    x = F.grid_sample(x, grid)
    return x
    
class Transform9(nn.Module): # sharpen
  def __init__(self):
    super(Transform9, self).__init__()
    self.conv1 = nn.Conv2d(3, 3, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False, groups=3)
    kernel = [[-1, -1, -1], 
              [-1,  9, -1], 
              [-1, -1, -1]]
    kernel = torch.from_numpy(np.array(kernel)).float().view(1,3,3)
    kernel = torch.stack([kernel] * 3).cuda()
    self.conv1.weight = nn.Parameter(kernel)
    self.conv1.requires_grad = False
  
  def forward(self, x):
    return self.conv1(x)
    
class Transform10(nn.Module): # smooth
  def __init__(self):
    super(Transform10, self).__init__()
    kernel = [[1, 2, 1],
              [2, 4, 1],
              [1, 2, 1]] # Gaussian smoothing
    kernel = torch.from_numpy(np.array(kernel)).float().view(1,3,3) * 0.0625
    kernel = torch.stack([kernel] * 3).cuda()
    self.conv1 = nn.Conv2d(3, 3, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False, groups=3)
    self.conv1.weight = nn.Parameter(kernel)
    self.conv1.requires_grad = False
  
  def forward(self, x):
    return self.conv1(x)
    
class Transform(nn.Module): # random transform combination
  def __init__(self):
    super(Transform, self).__init__()
    self.T2  = Transform2()
    self.T4  = Transform4()
    self.T6  = Transform6()
    self.T7  = Transform7()
    self.T9  = Transform9()
    self.T10 = Transform10()
    self.transforms = []
    for name in dir(self):
      if name[0] == "T" and name[1:].isdigit():
        self.transforms.append(eval("self.%s" % name))
    self.transforms = np.array(self.transforms)
    print(self.transforms)
    
  def forward(self, y):
    rand = np.random.permutation(len(self.transforms))
    Ts = self.transforms[rand]
    for T in Ts:
      if np.random.rand() >= 0.5:
        y = T(y)
    return y    

################# Transform #################
class AutoEncoder_GAN4(nn.Module):
  def __init__(self, args):
    super(AutoEncoder_GAN4, self).__init__()
    if args.dataset == "CIFAR10":
      BE = VGG19; Dec = DVGG19_deconv; SE = SmallVGG19
      self.normalize = Normalize_CIFAR10()
    elif args.dataset == "MNIST":
      Dec = DLeNet5_deconv
      mark_be = int(args.deep_lenet5[0]) * "_deep"
      mark_se = int(args.deep_lenet5[1]) * "_deep"
      BE = eval("LeNet5" + mark_be)
      SE = eval("SmallLeNet5" + mark_se)
      self.normalize = Normalize_MNIST()
    
    self.be = BE(args.e1, fixed=True)
    self.defined_trans = Transform()
    self.upscale = nn.UpsamplingNearest2d(scale_factor=2)
    self.bn1 = nn.BatchNorm2d(32)
    self.bn2 = nn.BatchNorm2d(32)
    self.bn3 = nn.BatchNorm2d(16)
    self.bn4 = nn.BatchNorm2d( 3)
    
    input_dim = args.num_z + args.num_class if args.use_condition else args.num_z
    self.codemap = CodeMapping(input_dim)
    
    for di in range(1, args.num_dec + 1):
      pretrained_model = None
      if args.pretrained_dir:
        assert(args.pretrained_timeid != None)
        pretrained_model = [x for x in os.listdir(args.pretrained_dir) if "_d%s_" % di in x and args.pretrained_timeid in x] # the number of pretrained decoder should be like "SERVER218-20190313-1233_d3_E0S0.pth"
        assert(len(pretrained_model) == 1)
        pretrained_model = pretrained_model[0]
      self.__setattr__("d" + str(di), Dec(input_dim, pretrained_model, fixed=False, gray=args.gray, num_divbranch=args.num_divbranch))
      self.mask = MaskNet(input_dim)
      self.meta = MetaNet(input_dim)
    
    for sei in range(1, args.num_se + 1):
      self.__setattr__("se" + str(sei), SE(args.e2, fixed=False))
      
AutoEncoders = {
"GAN4": AutoEncoder_GAN4,
}
  