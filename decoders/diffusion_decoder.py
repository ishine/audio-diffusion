import torch
from torch import nn
from torch.nn import functional as F
from blocks.blocks import DBlock, MappingNet, SkipBlock, FourierFeatures, UBlock, UNet, expand_to_planes, SelfAttention1d, ResConvBlock, Downsample1d, Upsample1d
from blocks.utils import append_dims

class DiffusionResConvUnet(nn.Module):
    def __init__(self, latent_dim, io_channels, depth=16):
        super().__init__()
        max_depth = 16
        depth = min(depth, max_depth)
        c_mults = [256, 256, 512, 512] + [512] * 12
        c_mults = c_mults[:depth]

        self.io_channels = io_channels
        self.timestep_embed = FourierFeatures(1, 16)
        block = nn.Identity()
        for i in range(depth, 0, -1):
            c = c_mults[i - 1]
            if i > 1:
                c_prev = c_mults[i - 2]
                block = SkipBlock(
                    nn.AvgPool1d(2),
                    ResConvBlock(c_prev, c, c),
                    ResConvBlock(c, c, c),
                    ResConvBlock(c, c, c),
                    block,
                    ResConvBlock(c * 2 if i != depth else c, c, c),
                    ResConvBlock(c, c, c),
                    ResConvBlock(c, c, c_prev),
                    nn.Upsample(scale_factor=2, mode='linear', align_corners=False),
                )
            else:
                block = nn.Sequential(
                    ResConvBlock(io_channels + 16 + latent_dim, c, c),
                    ResConvBlock(c, c, c),
                    ResConvBlock(c, c, c),
                    block,
                    ResConvBlock(c * 2, c, c),
                    ResConvBlock(c, c, c),
                    ResConvBlock(c, c, io_channels, is_last=True),
                )
        self.net = block

    def forward(self, input, t, quantized):
        timestep_embed = expand_to_planes(self.timestep_embed(t[:, None]), input.shape)
        quantized = F.interpolate(quantized, (input.shape[2], ), mode='linear', align_corners=False)
        return self.net(torch.cat([input, timestep_embed, quantized], dim=1))


class DiffusionAttnUnet1D(nn.Module):
    def __init__(
        self, 
        global_args, 
        io_channels = 2, 
        depth=14, 
        n_attn_layers = 6,
        c_mults = [128, 128, 256, 256] + [512] * 10
    ):
        super().__init__()

        # max_depth = 
        # depth = min(depth, max_depth)

        self.timestep_embed = FourierFeatures(1, 16)

        attn_layer = depth - n_attn_layers - 1

        block = nn.Identity()

        conv_block = ResConvBlock

        for i in range(depth, 0, -1):
            c = c_mults[i - 1]
            if i > 1:
                c_prev = c_mults[i - 2]
                add_attn = i >= attn_layer and n_attn_layers > 0
                block = SkipBlock(
                    Downsample1d("cubic"),
                    conv_block(c_prev, c, c),
                    SelfAttention1d(
                        c, c // 32) if add_attn else nn.Identity(),
                    conv_block(c, c, c),
                    SelfAttention1d(
                        c, c // 32) if add_attn else nn.Identity(),
                    conv_block(c, c, c),
                    SelfAttention1d(
                        c, c // 32) if add_attn else nn.Identity(),
                    block,
                    conv_block(c * 2 if i != depth else c, c, c),
                    SelfAttention1d(
                        c, c // 32) if add_attn else nn.Identity(),
                    conv_block(c, c, c),
                    SelfAttention1d(
                        c, c // 32) if add_attn else nn.Identity(),
                    conv_block(c, c, c_prev),
                    SelfAttention1d(c_prev, c_prev //
                                    32) if add_attn else nn.Identity(),
                    Upsample1d(kernel="cubic")
                    # nn.Upsample(scale_factor=2, mode='linear',
                    #             align_corners=False),
                )
            else:
                block = nn.Sequential(
                    conv_block(io_channels + 16 + global_args.latent_dim, c, c),
                    conv_block(c, c, c),
                    conv_block(c, c, c),
                    block,
                    conv_block(c * 2, c, c),
                    conv_block(c, c, c),
                    conv_block(c, c, io_channels, is_last=True),
                )
        self.net = block

        with torch.no_grad():
            for param in self.net.parameters():
                param *= 0.5

    def forward(self, input, t, cond=None):
        timestep_embed = expand_to_planes(self.timestep_embed(t[:, None]), input.shape)
        
        inputs = [input, timestep_embed]

        if cond is not None:
            cond = F.interpolate(cond, (input.shape[2], ), mode='linear', align_corners=False)
            inputs.append(cond)

        return self.net(torch.cat(inputs, dim=1))


class AudioDenoiserModel(nn.Module):
    def __init__(
        self, 
        c_in,
        feats_in,
        depths, 
        channels,
        self_attn_depths,
        mapping_cond_dim=0,
        unet_cond_dim=0,
        dropout_rate=0.
    ):
        super().__init__()
        self.timestep_embed = FourierFeatures(1, feats_in)
        if mapping_cond_dim > 0:
            self.mapping_cond = nn.Linear(mapping_cond_dim, feats_in, bias=False)
        self.mapping = MappingNet(feats_in, feats_in)
        self.proj_in = nn.Conv1d(c_in + unet_cond_dim, channels[0], 1)
        self.proj_out = nn.Conv1d(channels[0], c_in, 1)
        nn.init.zeros_(self.proj_out.weight)
        nn.init.zeros_(self.proj_out.bias)
        d_blocks, u_blocks = [], []
        for i in range(len(depths)):
            my_c_in = channels[i] if i==0 else channels[i-1]
            d_blocks.append(DBlock(depths[i], feats_in, my_c_in, channels[i], channels[i], downsample=i>0, self_attn=self_attn_depths[i], dropout_rate=dropout_rate))

        for i in range(len(depths)):
            my_c_in = channels[i] * 2 if i < len(depths) - 1 else channels[i]
            my_c_out = channels[i] if i == 0 else channels[i - 1]
            u_blocks.append(UBlock(depths[i], feats_in, my_c_in, channels[i], my_c_out, upsample=i > 0, self_attn=self_attn_depths[i], dropout_rate=dropout_rate))
        self.u_net = UNet(d_blocks, reversed(u_blocks))

    def forward(self, input, sigma, mapping_cond=None, unet_cond=None, log_sigma = True):
        if log_sigma:
            sigma = sigma.log() / 4
        c_noise = sigma
        timestep_embed = self.timestep_embed(append_dims(c_noise, 2))
        mapping_cond_embed = torch.zeros_like(timestep_embed) if mapping_cond is None else self.mapping_cond(mapping_cond)
        
        mapping_out = self.mapping(timestep_embed + mapping_cond_embed)
        cond = {'cond': mapping_out}
        if unet_cond is not None:
            input = torch.cat([input, unet_cond], dim=1)
        input = self.proj_in(input)
        input = self.u_net(input, cond)
        input = self.proj_out(input)
        return input
        
class Denoiser(nn.Module):
    """A Karras et al. preconditioner for denoising diffusion models."""

    def __init__(self, inner_model, sigma_data=1.):
        super().__init__()
        self.inner_model = inner_model
        self.sigma_data = sigma_data

    def get_scalings(self, sigma):
        c_skip = self.sigma_data**2 / (sigma**2 + self.sigma_data**2)
        c_out = sigma * self.sigma_data / (sigma**2 + self.sigma_data**2)**0.5
        c_in = 1 / (sigma**2 + self.sigma_data**2)**0.5
        return c_skip, c_out, c_in

    def loss(self, input, noise, sigma, **kwargs):
        c_skip, c_out, c_in = [append_dims(x, input.ndim) for x in self.get_scalings(sigma)]
        noised_input = input + noise * append_dims(sigma, input.ndim)
        model_output = self.inner_model(noised_input * c_in, sigma, **kwargs)
        target = (input - c_skip * noised_input) / c_out
        return (model_output - target).pow(2).flatten(1).mean(1)

    def forward(self, input, sigma, **kwargs):
        c_skip, c_out, c_in = [append_dims(x, input.ndim) for x in self.get_scalings(sigma)]
        return self.inner_model(input * c_in, sigma, **kwargs) * c_out + input * c_skip
