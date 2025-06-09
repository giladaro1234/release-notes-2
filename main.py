import os
import requests
from bs4 import BeautifulSoup
import hashlib
from flask import Flask, request
import google.generativeai as genai
from google.cloud import storage

app = Flask(__name__)

# --- Configuration ---
RELEASE_NOTES_URL = "https://cloud.google.com/release-notes"
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", " ai-release-notes")
STATE_FILE_NAME = "release_notes_hash.txt"
CHAT_WEBHOOK_URL = os.environ.get("CHAT_WEBHOOK_URL") # Get webhook URL from environment variables

# --- Configure Gemini API ---
# Store your API Key securely, e.g., as a Secret Manager secret mounted in Cloud Run
# For this example, we'll use an environment variable.
API_KEY = os.environ.get("GEMINI_API_KEY")
if API_KEY:
    genai.configure(api_key=API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
else:
    model = None

# --- Cloud Storage Client ---
storage_client = storage.Client()
bucket = storage_client.bucket(GCS_BUCKET_NAME)

def get_previous_hash():
    """Fetches the last known hash from the GCS bucket."""
    try:
        blob = bucket.blob(STATE_FILE_NAME)
        return blob.download_as_text()
    except Exception as e:
        print(f"Could not retrieve previous hash (this is normal on the first run): {e}")
        return None

def set_new_hash(new_hash):
    """Saves the new hash to the GCS bucket."""
    blob = bucket.blob(STATE_FILE_NAME)
    blob.upload_from_string(new_hash)
    print(f"Updated hash in GCS to: {new_hash}")

def send_to_google_chat(summary):
    """Sends a formatted message to a Google Chat webhook."""
    if not CHAT_WEBHOOK_URL:
        print("CHAT_WEBHOOK_URL not set. Skipping notification.")
        return
        
    message = {
        "text": f"*New Google Cloud Release Notes Summary for June 9, 2025*\n\n{summary}"
    }
    try:
        response = requests.post(CHAT_WEBHOOK_URL, json=message)
        response.raise_for_status()
        print("Successfully sent notification to Google Chat.")
    except Exception as e:
        print(f"Error sending to Google Chat: {e}")

@app.route('/', methods=['POST'])
def process_release_notes():
    """Main function triggered by Cloud Scheduler."""
    print("Cloud Run service triggered. Starting release notes check...")
    
    # 1. Fetch live content from the URL
    try:
        response = requests.get(RELEASE_NOTES_URL, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching URL: {e}")
        return "Error fetching URL", 500

    # 2. Parse the HTML and calculate the new hash
    soup = BeautifulSoup(response.content, 'html.parser')
    # Find the main content area (you might need to adjust the selector)
    main_content = soup.find('main') or soup.find('body')
    current_content_text = main_content.get_text()
    current_hash = hashlib.sha256(current_content_text.encode('utf-8')).hexdigest()
    print(f"Current content hash: {current_hash}")

    # 3. Compare with the previous hash
    previous_hash = get_previous_hash()
    
    if current_hash == previous_hash:
        print("No changes detected on the release notes page.")
        return "No changes detected.", 200

    print("Change detected! Generating summary...")

    # 4. If different, use Gemini to generate a summary
    if not model:
        print("Gemini API key not configured. Cannot generate summary.")
        return "API key missing", 500

    prompt = f"The Google Cloud release notes page has been updated. Based on the following text, please provide a concise, bulleted summary of the LATEST changes. Focus only on what seems new.\n\nDOCUMENT TEXT:\n{current_content_text[:10000]}" # Use a slice to not exceed model limits

    try:
        gemini_response = model.generate_content(prompt)
        summary = gemini_response.text
    except Exception as e:
        print(f"Error calling Gemini API: {e}")
        return "Error calling Gemini API", 500

    print(f"Generated Summary:\n{summary}")

    # 5. Send the summary to a destination
    send_to_google_chat(summary)

    # 6. Update the hash in GCS for the next run
    set_new_hash(current_hash)
    
    return "Successfully processed updates.", 200

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
