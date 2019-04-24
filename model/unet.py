"""

"""


# Built-in
import os
import copy
import time

# Libs
import torch
import torchvision
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
from torch.utils import data
from tensorboardX import SummaryWriter

# Own modules
from data import data_loader
from data import patch_extractor
from mrs_utils import misc_utils
from mrs_utils import metric_utils


class up(nn.Module):
    """
    A class for creating neural network blocks containing layers:

    Bilinear interpolation --> Convlution + Leaky ReLU --> Convolution + Leaky ReLU

    This is used in the UNet Class to create a UNet like NN architecture.
    ...
    Methods
    -------
    forward(x, skpCn)
        Returns output tensor after passing input `x` to the neural network
        block.
    """

    def __init__(self, inChannels, outChannels):
        """
        Parameters
        ----------
            inChannels : int
                number of input channels for the first convolutional layer.
            outChannels : int
                number of output channels for the first convolutional layer.
                This is also used for setting input and output channels for
                the second convolutional layer.
        """

        super(up, self).__init__()
        # Initialize convolutional layers.
        self.conv1 = nn.Conv2d(inChannels, outChannels, 3, stride=1, padding=1)
        # (2 * outChannels) is used for accommodating skip connection.
        self.conv2 = nn.Conv2d(2 * outChannels, outChannels, 3, stride=1, padding=1)

    def forward(self, x, skpCn):
        """
        Returns output tensor after passing input `x` to the neural network
        block.
        Parameters
        ----------
            x : tensor
                input to the NN block.
            skpCn : tensor
                skip connection input to the NN block.
        Returns
        -------
            tensor
                output of the NN block.
        """

        # Bilinear interpolation with scaling 2.
        x = F.interpolate(x, scale_factor=2, mode='bilinear')
        # Convolution + Leaky ReLU
        x = F.leaky_relu(self.conv1(x), negative_slope=0.1)
        # Convolution + Leaky ReLU on (`x`, `skpCn`)
        x = F.leaky_relu(self.conv2(torch.cat((x, skpCn), 1)), negative_slope=0.1)
        return x


def iou_pytorch(outputs: torch.Tensor, labels: torch.Tensor, smooth=1e-6):
    # You can comment out this line if you are passing tensors of equal shape
    # But if you are passing output from UNet or something it will most probably
    # be with the BATCH x 1 x H x W shape
    #outputs = outputs.squeeze(1)  # BATCH x 1 x H x W => BATCH x H x W
    outputs = torch.argmax(outputs, dim=1)
    intersection = (outputs & labels).float().sum((1, 2))  # Will be zero if Truth=0 or Prediction=0
    union = (outputs | labels).float().sum((1, 2))  # Will be zzero if both are 0

    iou = (intersection + smooth) / (union + smooth)  # We smooth our devision to avoid 0/0
    # thresholded = torch.clamp(20 * (iou - 0.5), 0, 10).ceil() / 10  # This is equal to comparing with thresolds
    return iou.mean()


class EncoderRes101(nn.Module):
    def __init__(self, predir=None):
        super(EncoderRes101, self).__init__()
        if not predir:
            encoder = list(torchvision.models.resnet101(pretrained=True).children())
        else:
            encoder = UFER('res101')
            encoder.load_state_dict(torch.load(predir))
            encoder = list(encoder.children())[0]
        self.conv1 = nn.Sequential(*encoder[:5])
        self.conv2 = encoder[5]
        self.conv3 = encoder[6]
        self.conv4 = encoder[7]

    def forward(self, x):
        s1 = self.conv1(x)
        s2 = self.conv2(s1)
        s3 = self.conv3(s2)
        s4 = self.conv4(s3)
        return s1, s2, s3, s4


class DecoderRes101(nn.Module):
    def __init__(self, n_class):
        super(DecoderRes101, self).__init__()
        self.up1 = up(2048, 1024)
        self.up2 = up(1024, 512)
        self.up3 = up(512, 256)
        self.final_conv1 = nn.Conv2d(256, 128, 3, padding=1)
        self.final_conv2 = nn.Conv2d(128, 128, 3, padding=1)
        self.classify = nn.Conv2d(128, n_class, 1)

    def forward(self, s1, s2, s3, s4):
        x = self.up1.forward(s4, s3)
        x = self.up2.forward(x, s2)
        x = self.up3.forward(x, s1)
        x = F.interpolate(x, scale_factor=4, mode='bilinear')
        x = self.final_conv1(x)
        x = self.final_conv2(x)
        x = self.classify(x)
        return x


class Unet(nn.Module):
    # TODO load pretrained resnet
    def __init__(self, encoder_name, n_class, predir=None):
        super(Unet, self).__init__()
        self.encoder_name = misc_utils.stem_string(encoder_name)
        self.n_class = n_class
        if self.encoder_name == 'res101':
            self.encoder = EncoderRes101(predir)
            self.decoder = DecoderRes101(self.n_class)
        else:
            raise NotImplementedError('Encoder name {} not recognized'.format(self.encoder_name))

    def forward(self, x):
        if self.encoder_name == 'res101':
            s1, s2, s3, s4 = self.encoder.forward(x)
            x = self.decoder.forward(s1, s2, s3, s4)
            return x
        else:
            raise NotImplementedError('Encoder name {} not recognized'.format(self.encoder_name))

    @staticmethod
    def decode_labels(label):
        # TODO label color dict as input parameter
        def decode_(a, color_dict):
            return color_dict[a]

        label_colors = {0: (255, 255, 255), 1: (0, 0, 255), 2: (0, 255, 255), 3: (255, 0, 0),
                        4: (255, 255, 0), 5: (0, 255, 0)}
        vfunc = np.vectorize(decode_)
        rgb_label = vfunc(label, label_colors)
        return np.dstack(rgb_label) / 255

    def mask_to_rgb(self, tensor_mask):
        tensor_mask = tensor_mask.cpu().data.numpy()
        tt = torchvision.transforms.ToTensor()
        return tt(self.decode_labels(tensor_mask)).float()

    def train_model(self, device, epochs, alpha, optm, criterion, scheduler, reader, save_dir,
          summary_path, rev_transform, save_epoch=5, verb_step=100):
        # TODO record learning rate
        writer = SummaryWriter(summary_path)
        best_model_wts = copy.deepcopy(self.state_dict())
        best_loss = 0.0
        step = 0

        for epoch in range(epochs):
            print('Epoch {}/{}'.format(epoch, epochs - 1))
            print('-' * 10)

            for phase in ['train', 'valid']:
                start_time = time.time()
                running_loss = 0.0
                running_iou = 0.0
                if phase == 'train':
                    scheduler.step()
                    self.train()  # Set model to training mode
                else:
                    self.eval()  # Set model to evaluate mode

                reader_cnt = 1
                ftr, lbl, pred = None, None, None
                for ftr, lbl in reader[phase]:
                    reader_cnt += 1

                    if phase == 'train':
                        step += 1

                    ftr = ftr.to(device)
                    lbl = torch.squeeze(lbl, dim=1).long().to(device)
                    pred = self.forward(ftr)

                    # zero the parameter gradients
                    optm.zero_grad()

                    # loss = criterion(pred, lbl)
                    loss = metric_utils.weighted_jaccard_loss(pred, lbl, criterion, alpha)
                    iou = iou_pytorch(pred, lbl)

                    # backward + optimize only if in training phase
                    if phase == 'train':
                        loss.backward()
                        optm.step()

                    running_loss = running_loss * (reader_cnt - 1) / reader_cnt + loss.item() / reader_cnt
                    running_iou = running_iou * (reader_cnt - 1) / reader_cnt + iou.item() / reader_cnt
                    if phase == 'train' and reader_cnt % verb_step == 0:
                        writer.add_scalar('loss_train', loss.item(), step)
                        writer.add_scalar('iou_train', iou.item(), step)
                        elapsed = time.time() - start_time
                        print('Epoch {}, {} Step:{} Loss: {:.4f}, IoU: {:.4f}, Duration: {:.0f}m {:.0f}s'.format(
                            epoch, phase, reader_cnt, loss.item(), iou.item(), elapsed // 60, elapsed % 60))

                if phase == 'valid':
                    for img_cnt in range(ftr.shape[0]):
                        lbl_img = self.mask_to_rgb(lbl[img_cnt])
                        pred_img = self.mask_to_rgb(torch.argmax(pred[img_cnt].cpu(), dim=0))

                        tb_img = torchvision.utils.make_grid(
                            [rev_transform(ftr[img_cnt].cpu()), lbl_img, pred_img])
                        writer.add_image('image_valid_{}'.format(img_cnt), tb_img, epoch)
                    writer.add_scalar('loss_valid', running_loss, epoch)
                    writer.add_scalar('iou_valid', running_iou, epoch)
                    writer.add_scalar('lr_encoder', scheduler.get_lr()[0], epoch)
                    writer.add_scalar('lr_encoder', scheduler.get_lr()[1], epoch)
                    elapsed = time.time() - start_time
                    print('Epoch {}, {} Step:{} Loss: {:.4f}, IoU: {:.4f}, Duration: {:.0f}m {:.0f}s'.format(
                        epoch, phase, reader_cnt, running_loss, running_iou, elapsed // 60, elapsed % 60))
                    if running_loss < best_loss:
                        # deep copy the model
                        best_loss = running_loss
                        best_model_wts = copy.deepcopy(self.state_dict())

            if epoch % save_epoch == 0:
                torch.save(self.state_dict(), os.path.join(save_dir, 'model_{}.pt'.format(epoch)))
        self.load_state_dict(best_model_wts)

    def eval_tile(self, img, input_size, batch_size, pad, device, transforms):
        self.eval()
        tile_size = img.shape[:2]
        reader = data_loader.TileDataset(img, input_size, pad, transforms)
        reader = data.DataLoader(reader, batch_size=batch_size, shuffle=False, num_workers=batch_size, drop_last=False)

        # evaluate tile
        tile_pred = []
        for patch in reader:
            patch = patch.to(device)
            pred = self.forward(patch).data.cpu().numpy()
            pred = np.transpose(pred, (0, 2, 3, 1))
            tile_pred.append(pred)
        tile_pred = np.concatenate(tile_pred, axis=0)
        tile_pred = patch_extractor.unpatch_block(tile_pred, tile_size, input_size, tile_size, input_size, 0)
        return np.argmax(tile_pred, axis=-1)


class UFER(nn.Module):
    def __init__(self, encoder_name):
        super(UFER, self).__init__()
        encoder_name = misc_utils.stem_string(encoder_name)
        if encoder_name == 'res101':
            encoder = torchvision.models.resnet101(pretrained=True)
            self.encoder = nn.Sequential(*list(encoder.children())[:-1])
        else:
            raise NotImplementedError('Encoder name {} not recognized'.format(encoder_name))

    def forward(self, x):
        ftr = self.encoder(x)
        ftr = torch.reshape(ftr, (-1, 2048))
        return ftr



if __name__ == '__main__':
    unet = Unet('res101', 2)
    sample = torch.rand((5, 3, 224, 224))
    device = misc_utils.set_gpu(1)
    sample.to(device)
    unet.forward(sample)
