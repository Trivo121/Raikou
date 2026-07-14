#!/bin/bash
sudo docker rm -f sarchat-vllm 2>/dev/null
sudo docker run -d --gpus all \
  -v /home/ubuntu/backend/models:/models \
  -p 8001:8000 \
  --name sarchat-vllm \
  vllm/vllm-openai:latest \
  --model /models/SARChat-Phi-3.5-vision-instruct \
  --trust-remote-code \
  --gpu-memory-utilization 0.85 \
  --max-model-len 8192 \
  --max-num-seqs 2 \
  --mm-processor-kwargs '{"num_crops": 4}' \
  --limit-mm-per-prompt '{"image": 9}'
