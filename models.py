import requests
import os
url = "https://api.groq.com/openai/v1/models"
headers = {"Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}"}
resp = requests.get(url, headers=headers)
print([m["id"] for m in resp.json()["data"]])
