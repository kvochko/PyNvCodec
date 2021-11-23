#
# Copyright 2019 NVIDIA Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# Starting from Python 3.8 DLL search policy has changed.
# We need to add path to CUDA DLLs explicitly.
import multiprocessing
import sys
import os
import threading
from typing import Dict

if os.name == 'nt':
    # Add CUDA_PATH env variable
    cuda_path = os.environ["CUDA_PATH"]
    if cuda_path:
        os.add_dll_directory(cuda_path)
    else:
        print("CUDA_PATH environment variable is not set.", file=sys.stderr)
        print("Can't set CUDA DLLs search path.", file=sys.stderr)
        exit(1)

    # Add PATH as well for minor CUDA releases
    sys_path = os.environ["PATH"]
    if sys_path:
        paths = sys_path.split(';')
        for path in paths:
            if os.path.isdir(path):
                os.add_dll_directory(path)
    else:
        print("PATH environment variable is not set.", file=sys.stderr)
        exit(1)

import PyNvCodec as nvc
from enum import Enum
import numpy as np
import av

from multiprocessing import Process
import subprocess
import uuid


def get_stream_params(url: str) -> Dict:
    params = {}

    input_container = av.open(url)
    in_stream = input_container.streams.video[0]

    params['width'] = in_stream.codec_context.width
    params['height'] = in_stream.codec_context.height
    params['framerate'] = in_stream.codec_context.framerate.numerator / \
        in_stream.codec_context.framerate.denominator

    is_h264 = True if in_stream.codec_context.name == 'h264' else False
    is_hevc = True if in_stream.codec_context.name == 'hevc' else False
    if not is_h264 and not is_hevc:
        raise ValueError("Unsupported codec: " + in_stream.codec_context.name +
                         '. Only H.264 and HEVC are supported in this sample.')
    else:
        params['codec'] = nvc.CudaVideoCodec.H264 if is_h264 else nvc.CudaVideoCodec.HEVC

    is_yuv420 = in_stream.codec_context.pix_fmt == 'yuv420p'
    is_yuv444 = in_stream.codec_context.pix_fmt == 'yuv444p'
    if not is_yuv420 and not is_yuv444:
        raise ValueError("Unsupported pixel format: " +
                         in_stream.codec_context.pix_fmt +
                         '. Only YUV420 and YUV444 are supported in this sample.')
    else:
        params['format'] = nvc.PixelFormat.NV12 if is_yuv420 else nvc.PixelFormat.YUV444

    return params


def rtsp_client(url: str, name: str, gpu_id: int) -> None:
    # Get stream parameters
    params = get_stream_params(url)
    w = params['width']
    h = params['height']
    f = params['format']
    c = params['codec']
    g = gpu_id

    # Prepare ffmpeg arguments
    if nvc.CudaVideoCodec.H264 == c:
        codec_name = 'h264'
    elif nvc.CudaVideoCodec.HEVC == c:
        codec_name = 'hevc'
    bsf_name = codec_name + '_mp4toannexb,dump_extra=all'

    cmd = [
        'ffmpeg',       '-hide_banner',
        '-i',           url,
        '-c:v',         'copy',
        '-bsf:v',       bsf_name,
        '-f',           codec_name,
        'pipe:1'
    ]
    # Run ffmpeg in subprocess and redirect it's output to pipe
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)

    # Create HW decoder class
    nvdec = nvc.PyNvDecoder(w, h, f, c, g)

    # Amount of bytes we read from pipe first time.
    read_size = 4096
    # Total bytes read and total frames decded to get average data rate
    rt = 0
    fd = 0

    # Main decoding loop, will not flush intentionally because don't know the
    # amount of frames available via RTSP.
    while True:
        # Pipe read underflow protection
        if not read_size:
            read_size = int(rt / fd)
            # Counter overflow protection
            rt = read_size
            fd = 1

        # Read data.
        # Amount doesn't really matter, will be updated later on during decode.
        bits = proc.stdout.read(read_size)
        if not len(bits):
            print("Can't read data from pipe")
            break
        else:
            rt += len(bits)

        # Decode
        enc_packet = np.frombuffer(buffer=bits, dtype=np.uint8)
        pkt_data = nvc.PacketData()
        try:
            surf = nvdec.DecodeSurfaceFromPacket(enc_packet, pkt_data)

            if not surf.Empty():
                fd += 1
                # Shifts towards underflow to avoid increasing vRAM consumption.
                if pkt_data.bsl < read_size:
                    read_size = pkt_data.bsl
                # Print process ID every second or so.
                fps = int(params['framerate'])
                if not fd % fps:
                    print(name)

        # Handle HW exceptions in simplest possible way by decoder respawn
        except nvc.HwResetException:
            nvdec = nvc.PyNvDecoder(w, h, f, c, g)
            continue


if __name__ == "__main__":
    print("This sample decodes multiple videos in parallel on given GPU.")
    print("It doesn't do anything beside decoding, output isn't saved.")
    print("Usage: SampleDecodeRTSP.py $gpu_id $url1 ... $urlN .")

    if(len(sys.argv) < 3):
        print("Provide gpu ID and input URL(s).")
        exit(1)

    gpuID = int(sys.argv[1])
    urls = []

    for i in range(2, len(sys.argv)):
        urls.append(sys.argv[i])

    pool = []
    for url in urls:
        client = Process(target=rtsp_client, args=(
            url, str(uuid.uuid4()), gpuID))
        client.start()
        pool.append(client)

    for client in pool:
        client.join()
