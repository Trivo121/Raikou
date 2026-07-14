import requests
import json
import time

b64_str = "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="

def count_text_tokens(text):
    # Rough approximation or exact if tokenizer is available
    return len(text.split())

def test_payload(name, num_images):
    print(f"Testing {name} with {num_images} images...")
    
    prompt_text = "Describe these images."
    content = [{"type": "text", "text": prompt_text}]
    for _ in range(num_images):
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_str}"}})
        
    messages = [{"role": "user", "content": content}]
    
    payload = {
        "model": "/models/SARChat-Phi-3.5-vision-instruct",
        "messages": messages,
        "max_tokens": 10
    }
    
    try:
        start = time.time()
        resp = requests.post("http://localhost:8001/v1/chat/completions", json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        
        # Phi-3.5-vision adds some boilerplate tokens, but we can compare the diff
        print(f"Success {name} ({num_images} images): {prompt_tokens} total prompt tokens. Took {time.time()-start:.2f}s")
        return prompt_tokens
    except Exception as e:
        print(f"Error {name}:", e)
        return None

def main():
    t1 = test_payload("Single Image", 1)
    time.sleep(1)
    t4 = test_payload("Four Images", 4)
    
    if t1 and t4:
        diff = t4 - t1
        cost_per_image = diff / 3
        print(f"\nCalculated marginal cost per image: {cost_per_image:.1f} tokens")
        print(f"Base text + structural token overhead: {t1 - cost_per_image:.1f} tokens")

if __name__ == "__main__":
    main()
