# -*- coding: utf-8 -*-
# @Time    : 2024/5/8
# @Author  : White Jiang

import diffusers
from dressing_sd.pipelines.pipeline_sd import PipIpaControlNet
import os
import torch

from PIL import Image
from diffusers import ControlNetModel, UNet2DConditionModel, \
    AutoencoderKL, DDIMScheduler
from torchvision import transforms
from transformers import CLIPImageProcessor
from diffusers.pipelines.stable_diffusion import StableDiffusionSafetyChecker

from transformers import CLIPTextModel, CLIPTokenizer, CLIPVisionModelWithProjection
from adapter.attention_processor import CacheAttnProcessor2_0, RefSAttnProcessor2_0, \
    IPAttnProcessor2_0
import argparse
from adapter.resampler import Resampler


def image_grid(imgs, rows, cols):
    assert len(imgs) == rows * cols
    w, h = imgs[0].size
    grid = Image.new("RGB", size=(cols * w, rows * h))
    grid_w, grid_h = grid.size

    for i, img in enumerate(imgs):
        grid.paste(img, box=(i % cols * w, i // cols * h))
    return grid


def prepare(args):
    generator = torch.Generator(device=args.device).manual_seed(42)
    vae = AutoencoderKL.from_pretrained("stabilityai/sd-vae-ft-mse").to(dtype=torch.float16, device=args.device)
    tokenizer = CLIPTokenizer.from_pretrained("SG161222/Realistic_Vision_V4.0_noVAE", subfolder="tokenizer")
    text_encoder = CLIPTextModel.from_pretrained("SG161222/Realistic_Vision_V4.0_noVAE", subfolder="text_encoder").to(
        dtype=torch.float16, device=args.device)
    image_encoder = CLIPVisionModelWithProjection.from_pretrained("h94/IP-Adapter").to(
        dtype=torch.float16, device=args.device)
    unet = UNet2DConditionModel.from_pretrained("SG161222/Realistic_Vision_V4.0_noVAE", subfolder="unet").to(
        dtype=torch.float16,
        device=args.device)

    # load ipa weight
    image_proj = Resampler(
        dim=unet.config.cross_attention_dim,
        depth=4,
        dim_head=64,
        heads=12,
        num_queries=16,
        embedding_dim=image_encoder.config.hidden_size,
        output_dim=unet.config.cross_attention_dim,
        ff_mult=4
    )
    image_proj = image_proj.to(dtype=torch.float16, device=args.device)

    # set attention processor
    attn_procs = {}
    st = unet.state_dict()
    for name in unet.attn_processors.keys():
        cross_attention_dim = None if name.endswith("attn1.processor") else unet.config.cross_attention_dim
        if name.startswith("mid_block"):
            hidden_size = unet.config.block_out_channels[-1]
        elif name.startswith("up_blocks"):
            block_id = int(name[len("up_blocks.")])
            hidden_size = list(reversed(unet.config.block_out_channels))[block_id]
        elif name.startswith("down_blocks"):
            block_id = int(name[len("down_blocks.")])
            hidden_size = unet.config.block_out_channels[block_id]
        # lora_rank = hidden_size // 2 # args.lora_rank
        if cross_attention_dim is None:
            attn_procs[name] = RefSAttnProcessor2_0(name, hidden_size)
            layer_name = name.split(".processor")[0]
            weights = {
                "to_k_ref.weight": st[layer_name + ".to_k.weight"],
                "to_v_ref.weight": st[layer_name + ".to_v.weight"],
            }
            attn_procs[name].load_state_dict(weights)
        else:
            attn_procs[name] = IPAttnProcessor2_0(hidden_size=hidden_size, cross_attention_dim=cross_attention_dim)

    unet.set_attn_processor(attn_procs)
    adapter_modules = torch.nn.ModuleList(unet.attn_processors.values())
    adapter_modules = adapter_modules.to(dtype=torch.float16, device=args.device)
    del st

    ref_unet = UNet2DConditionModel.from_pretrained("SG161222/Realistic_Vision_V4.0_noVAE", subfolder="unet").to(
        dtype=torch.float16,
        device=args.device)
    ref_unet.set_attn_processor(
        {name: CacheAttnProcessor2_0() for name in ref_unet.attn_processors.keys()})  # set cache

    # weights load
    model_sd = torch.load(args.model_ckpt, map_location="cpu")["module"]

    ref_unet_dict = {}
    unet_dict = {}
    image_proj_dict = {}
    adapter_modules_dict = {}
    for k in model_sd.keys():
        if k.startswith("ref_unet"):
            ref_unet_dict[k.replace("ref_unet.", "")] = model_sd[k]
        elif k.startswith("unet"):
            unet_dict[k.replace("unet.", "")] = model_sd[k]
        elif k.startswith("proj"):
            image_proj_dict[k.replace("proj.", "")] = model_sd[k]
        elif k.startswith("adapter_modules") and 'ref' in k:
            adapter_modules_dict[k.replace("adapter_modules.", "")] = model_sd[k]
        else:
            print(k)

    ref_unet.load_state_dict(ref_unet_dict)
    image_proj.load_state_dict(image_proj_dict)
    adapter_modules.load_state_dict(adapter_modules_dict, strict=False)

    noise_scheduler = DDIMScheduler(
        num_train_timesteps=1000,
        beta_start=0.00085,
        beta_end=0.012,
        beta_schedule="scaled_linear",
        clip_sample=False,
        set_alpha_to_one=False,
        steps_offset=1,
    )

    control_net_openpose = ControlNetModel.from_pretrained("lllyasviel/control_v11p_sd15_openpose",
                                                           torch_dtype=torch.float16).to(device=args.device)
    pipe = PipIpaControlNet(unet=unet, reference_unet=ref_unet, vae=vae, tokenizer=tokenizer,
                            text_encoder=text_encoder, image_encoder=image_encoder,
                            ip_ckpt=args.ip_ckpt,
                            ImgProj=image_proj, controlnet=control_net_openpose,
                            scheduler=noise_scheduler,
                            safety_checker=StableDiffusionSafetyChecker,
                            feature_extractor=CLIPImageProcessor)
    return pipe, generator


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='ReferenceAdapter diffusion')
    parser.add_argument('--ip_ckpt',
                        default="path/to/IP-Adapter/ip-adapter-plus-face_sd15.bin",
                        type=str)
    parser.add_argument('--model_ckpt',
                        default="path/to/model.ckpt",
                        type=str)
    parser.add_argument('--cloth_path', type=str, required=True)
    parser.add_argument('--face_path', default=None, type=str)
    parser.add_argument('--pose_path', default=None, type=str)
    parser.add_argument('--output_path', type=str, default="./output_sd")
    parser.add_argument('--device', type=str, default="cuda:0")
    args = parser.parse_args()

    # svae path
    output_path = args.output_path

    if not os.path.exists(output_path):
        os.makedirs(output_path)

    pipe, generator = prepare(args)
    print('====================== pipe load finish ===================')

    num_samples = 1
    clip_image_processor = CLIPImageProcessor()

    img_transform = transforms.Compose([
        transforms.Resize([640, 512], interpolation=transforms.InterpolationMode.BILINEAR),
        transforms.ToTensor(),
        transforms.Normalize([0.5], [0.5]),
    ])

    prompt = 'A model wearing a colorful skirt'
    null_prompt = ''
    negative_prompt = 'bare, naked, nude, undressed, monochrome, lowres, bad anatomy, worst quality, low quality'

    clothes_img = Image.open(args.cloth_path).convert("RGB")
    vae_clothes = img_transform(clothes_img).unsqueeze(0)
    ref_clip_image = clip_image_processor(images=clothes_img, return_tensors="pt").pixel_values

    if args.face_path is not None:
        face_img = Image.open(args.face_path).convert("RGB")
        face_img.resize((256, 256))
        face_clip_image = clip_image_processor(images=face_img, return_tensors="pt").pixel_values
    else:
        face_clip_image = None

    if args.pose_path is not None:
        pose_image = diffusers.utils.load_image(args.pose_path)
    else:
        pose_image = None

    output = pipe(
        ref_image=vae_clothes,
        prompt=prompt,
        ref_clip_image=ref_clip_image,
        pose_image=pose_image,
        face_clip_image=face_clip_image,
        null_prompt=null_prompt,
        negative_prompt=negative_prompt,
        width=512,
        height=640,
        num_images_per_prompt=num_samples,
        guidance_scale=7.5,
        image_scale=1.0,
        ipa_scale=1.2,
        generator=generator,
        num_inference_steps=30,
    ).images

    save_output = []
    save_output.append(output[0])
    save_output.insert(0, clothes_img.resize((512, 640), Image.BICUBIC))

    grid = image_grid(save_output, 1, 2)
    grid.save(
        output_path + '/' + args.clothes_path.split("/")[-1])
