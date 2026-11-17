import os
import time
import urllib.parse
import hashlib
import hmac
import base64
import requests

api_key = os.environ["KRAKEN_API_KEY"]
api_sec = os.environ["KRAKEN_PRIVATE_KEY"]
api_url = "https://kraken.com"
uri_path = "/0/private/Balance"

data = {"nonce": str(int(1000 * time.time()))}
postdata = urllib.parse.urlencode(data)
encoded = (str(data["nonce"]) + postdata).encode()
message = uri_path.encode() + hashlib.sha256(encoded).digest()

mac = hmac.new(base64.b64decode(api_sec), message, hashlib.sha512)
sigdigest = base64.b64encode(mac.digest()).decode()

headers = {
    "API-Key": api_key,
    "API-Sign": sigdigest,
}

response = requests.post(api_url + uri_path, headers=headers, data=data)
print(response.json())
