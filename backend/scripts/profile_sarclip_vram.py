import os
import time
import subprocess
from app.services.models.sarclip_encoder import SARCLIPEncoder
from app.services.processing.patch_pipeline import plan_patches
import numpy as np

def run_smi_poll(duration=10):
    print(f"Starting nvidia-smi polling for {duration} seconds...")
    process = subprocess.Popen(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    max_mem = 0
    start = time.time()
    
    while time.time() - start < duration:
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                text=True
            ).strip()
            mem = int(out.split('\n')[0])
            if mem > max_mem:
                max_mem = mem
            time.sleep(0.5)
        except Exception:
            pass
            
    return max_mem

def main():
    print("Loading SARCLIPEncoder to GPU...")
    encoder = SARCLIPEncoder.get()
    
    # Generate some dummy data representing a batch of patches
    print("Generating dummy patches...")
    dummy_text = "test query"
    
    # Start polling VRAM usage
    import threading
    vram_result = [0]
    
    def poll():
        vram_result[0] = run_smi_poll(10)
        
    t = threading.Thread(target=poll)
    t.start()
    
    print("Running text encode...")
    for _ in range(5):
        encoder.encode_text(dummy_text)
        time.sleep(1)
        
    t.join()
    
    print(f"Peak VRAM usage during encoding: {vram_result[0]} MB")
    
if __name__ == "__main__":
    main()
