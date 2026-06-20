import os
import requests

api_key = os.environ.get("GROQ_API_KEY")
if not api_key:
    # Try reading it from backend/.env
    with open('backend/.env', 'r') as f:
        for line in f:
            if line.startswith('GROQ_API_KEY='):
                api_key = line.strip().split('=')[1]
                break

url = "https://api.groq.com/openai/v1/models"
headers = {
    "Authorization": f"Bearer {api_key}"
}
response = requests.get(url, headers=headers)
response.raise_for_status()
models = [m['id'] for m in response.json()['data']]
print("Available Groq Models:")
for m in models:
    print(m)
