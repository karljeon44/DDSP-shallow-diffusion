import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils import weight_norm

from ddsp_naive.core import Snake1d
from ddsp_naive.pcmer import PCmer


class Unit2MelNaive(nn.Module):
    def __init__(
            self,
            input_channel,
            n_spk,
            use_pitch_aug=False,
            out_dims=128,
            n_layers=3,
            n_chans=256,
            n_hidden=None,  # 废弃
            use_full_siren=False,
            l2reg_loss=0
    ):
        super().__init__()
        self.l2reg_loss = l2reg_loss if (l2reg_loss is not None) else 0
        self.f0_embed = nn.Linear(1, n_chans)
        self.volume_embed = nn.Linear(1, n_chans)
        if use_pitch_aug:
            self.aug_shift_embed = nn.Linear(1, n_chans, bias=False)
        else:
            self.aug_shift_embed = None

        self.n_spk = n_spk
        if n_spk is not None and n_spk > 1:
            self.spk_embed = nn.Embedding(n_spk, n_chans)

        # conv in stack
        print("Using Snake Activations")
        self.stack = nn.Sequential(
                nn.Conv1d(input_channel, n_chans, 3, 1, 1),
                nn.GroupNorm(4, n_chans),
                # nn.LeakyReLU(),
                Snake1d(n_chans),
                nn.Conv1d(n_chans, n_chans, 3, 1, 1))

        # transformer
        if use_full_siren:
            from .pcmer_siren_full import PCmer as PCmerfs
            self.decoder = PCmerfs(
                num_layers=n_layers,
                num_heads=8,
                dim_model=n_chans,
                dim_keys=n_chans,
                dim_values=n_chans,
                residual_dropout=0.1,
                attention_dropout=0.1)
        else:
            self.decoder = PCmer(
                num_layers=n_layers,
                num_heads=8,
                dim_model=n_chans,
                dim_keys=n_chans,
                dim_values=n_chans,
                residual_dropout=0.1,
                attention_dropout=0.1)
        self.norm = nn.LayerNorm(n_chans)

        # out
        self.n_out = out_dims
        self.dense_out = weight_norm(
            nn.Linear(n_chans, self.n_out))

    def forward(self, units, f0, volume, spk_id=None, spk_mix_dict=None, aug_shift=None, gt_spec=None, infer=True,
                infer_speedup=10, method='dpm-solver', k_step=None, use_tqdm=True, spk_emb=None, spk_emb_dict=None):

        '''
        input:
            B x n_frames x n_unit
        return:
            dict of B x n_frames x feat
        '''
        x = self.stack(units.transpose(1,2)).transpose(1,2)
        x = x + self.f0_embed((1+ f0 / 700).log()) + self.volume_embed(volume)

        if self.n_spk is not None and self.n_spk > 1:
            if spk_mix_dict is not None:
                for k, v in spk_mix_dict.items():
                    spk_id_torch = torch.LongTensor(np.array([[k]])).to(units.device)
                    x = x + v * self.spk_embed(spk_id_torch - 1)
            else:
                x = x + self.spk_embed(spk_id - 1)

        if self.aug_shift_embed is not None and aug_shift is not None:
            x = x + self.aug_shift_embed(aug_shift / 5)

        x = self.decoder(x)
        x = self.norm(x)
        x = self.dense_out(x)
        if not infer:
            x = F.mse_loss(x, gt_spec)
            if self.l2reg_loss > 0:
                x = x + l2_regularization(model=self, l2_alpha=self.l2reg_loss)
        return x


def l2_regularization(model, l2_alpha):
    l2_loss = []
    for module in model.modules():
        if type(module) is nn.Conv2d:
            l2_loss.append((module.weight ** 2).sum() / 2.0)
    return l2_alpha * sum(l2_loss)
