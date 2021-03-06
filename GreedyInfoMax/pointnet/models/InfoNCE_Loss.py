import torch
import torch.nn as nn
from torch.nn.modules.loss import _WeightedLoss
import torch.nn.functional as F

from GreedyInfoMax.utils import model_utils


class InfoNCE_Loss(nn.Module):
    """
        InfoNCE Loss adapted for 3-dimensional patch space instead of the original 2D
        When predicting up to K patches, we traverse the x-dim (does it matter? stochastic sampling of dim to choose?)
    """
    def __init__(self, opt, in_channels, out_channels):
        super().__init__()
        self.opt = opt
        self.negative_samples = self.opt.negative_samples
        self.k_predictions = self.opt.subcloud_cube_size-2
        self.ignore_index = -100

        self.W_k = nn.ModuleList(
            nn.Conv3d(in_channels, out_channels, 1, bias=False)
            for _ in range(self.k_predictions)
        )

        self.contrast_loss = ExpNLLLoss(ignore_index=self.ignore_index)

        if self.opt.weight_init:
            self.initialize()

    def initialize(self):
        for m in self.modules():
            if isinstance(m, (nn.Conv3d,)):
                if m in self.W_k:
                    model_utils.makeDeltaOrthogonal(
                        m.weight,
                        nn.init.calculate_gain(
                            "Sigmoid"
                        ),
                    )

    def forward(self, z, c, targets_to_ignore, skip_step=1):

        batch_size = z.shape[0]

        total_loss = 0

        if self.opt.device.type != "cpu":
            cur_device = z.get_device()
        else:
            cur_device = self.opt.device

        # For each element in c, contrast with elements below
        for dim in range(3):
            for k in range(1, self.k_predictions + 1):
                ### compute log f(c_t, x_{t+k}) = z^T_{t+k} W_k c_t
                # compute z^T_{t+k} W_k:
                if dim == 0:    # x
                    z_slice = z[:, :, (k + skip_step) :, :, :]
                elif dim == 1:  # y
                    z_slice = z[:, :, :, (k + skip_step):, :]
                else:           # z
                    z_slice = z[:, :, :, :, (k + skip_step):]
                #print("z slice shape:", z_slice.shape)
                ztwk = (
                    self.W_k[k - 1]
                    .forward(z_slice)  # Bx, C , x , y, z
                    .permute(2, 3, 4, 0, 1)  # x, y, z, Bx, C
                    .contiguous()
                )  # z, y, x, b, c
                #print("ztwk shape:", ztwk.shape)

                ztwk_shuf = ztwk.view(
                    ztwk.shape[0] * ztwk.shape[1] * ztwk.shape[2] * ztwk.shape[3], ztwk.shape[4]
                )  # z * y * x * batch, c
                rand_index = torch.randint(
                    ztwk_shuf.shape[0],  # z * y *  x * batch
                    (ztwk_shuf.shape[0] * self.negative_samples, 1),
                    dtype=torch.long,
                    device=cur_device,
                )
                # Sample more
                rand_index = rand_index.repeat(1, ztwk_shuf.shape[1])

                # get z^T_{j} W_k by shuffling z^T_{t+k} W_k over all points in batch
                ztwk_shuf = torch.gather(
                    ztwk_shuf, dim=0, index=rand_index, out=None
                )  # z * y * x * b * n, c

                ztwk_shuf = ztwk_shuf.view(
                    ztwk.shape[0],
                    ztwk.shape[1],
                    ztwk.shape[2],
                    ztwk.shape[3],
                    self.negative_samples,
                    ztwk.shape[4],
                ).permute(
                    0, 1, 2, 3, 5, 4
                )  # z, y, x, b, c, n

                #### Compute  x_W1 . c_t:
                if dim == 0:    # x
                    c_slice = c[:, :, : -(k + skip_step), :, :]
                elif dim == 1:  # y
                    c_slice = c[:, :, :, : -(k + skip_step), :]
                else:           # z
                    c_slice = c[:, :, :, :, : -(k + skip_step)]
                #print("c_slice shape:", c_slice.shape)
                context = (c_slice.permute(2, 3, 4, 0, 1).unsqueeze(-2))  # z, y, x, b, 1, c
                #print("context shape:", context.shape)

                log_fk_main = torch.matmul(context, ztwk.unsqueeze(-1)).squeeze(-2)  # z, y, x, b, 1

                log_fk_shuf = torch.matmul(context, ztwk_shuf).squeeze(-2)  # z, y, x, b, n

                log_fk = torch.cat((log_fk_main, log_fk_shuf), 4)  # z, y, x, b, 1+n
                log_fk = log_fk.permute(3, 4, 0, 1, 2)  # b, 1+n, z, y, x

                log_fk = torch.softmax(log_fk, dim=1)

                true_f = torch.zeros(
                    (batch_size, log_fk.shape[-3], log_fk.shape[-2], log_fk.shape[-1]),
                    dtype=torch.long,
                    device=cur_device,
                )  # b, z, y, x

                # Ignore cases where either of the two real patches have failed to contain any real points
                if dim == 0:    # x
                    ignore_slice = targets_to_ignore[:, : -(k + skip_step), :, :] + targets_to_ignore[:, (k + skip_step) :, :, :]
                elif dim == 1:  # y
                    ignore_slice = targets_to_ignore[:, :, : -(k + skip_step), :] + targets_to_ignore[:, :, (k + skip_step) :, :]
                else:           # z
                    ignore_slice = targets_to_ignore[:, :, :, : -(k + skip_step)] + targets_to_ignore[:, :, :, (k + skip_step) :]
                true_f[ignore_slice>0] = self.ignore_index

                total_loss += self.contrast_loss(input=log_fk, target=true_f)

        total_loss /= self.k_predictions*3

        return total_loss

class ExpNLLLoss(_WeightedLoss):

    def __init__(self, weight=None, size_average=None, ignore_index=-100,
                 reduce=None, reduction='mean'):
        super(ExpNLLLoss, self).__init__(weight, size_average, reduce, reduction)
        self.ignore_index = ignore_index

    def forward(self, input, target):
        x = torch.log(input + 1e-11)
        return F.nll_loss(x, target, weight=self.weight, ignore_index=self.ignore_index,
                          reduction=self.reduction)
