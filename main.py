import os
import subprocess
from pathlib import Path
from flask import Flask, request, send_from_directory, abort, render_template, make_response, Response, stream_with_context, jsonify, redirect
from yt_dlp import YoutubeDL
import time
from queue import Queue
from threading import Thread
import uuid
import json
from google.cloud import storage
from google.oauth2 import service_account
import feedparser
import sqlite3
from datetime import datetime
import threading

app = Flask(__name__)

BASE_DIR = "media"
DL_DIR = os.path.join(BASE_DIR, "downloads")
HLS_DIR = os.path.join(BASE_DIR, "hls")

Path(DL_DIR).mkdir(parents=True, exist_ok=True)
Path(HLS_DIR).mkdir(parents=True, exist_ok=True)

progress_queues = {}

class GCSUploader:
    def __init__(self, bucket_name):
        self.bucket_name = bucket_name
        creds = service_account.Credentials.from_service_account_file(
            "/home/davidd/.gcloud/connecthack.json"
        )
        self.client = storage.Client(credentials=creds, project=creds.project_id)

    def upload_file(self, local_file_path, gcs_path):
        bucket = self.client.bucket(self.bucket_name)
        blob = bucket.blob(gcs_path)
        
        generation_match_precondition = 0
        blob.upload_from_filename(
            local_file_path, 
            if_generation_match=generation_match_precondition
        )
        print(f"Uploaded {local_file_path} to gs://{self.bucket_name}/{gcs_path}")

def download_video(url):
    with YoutubeDL({
        'format': 'bestvideo[height=1080][ext=mp4]+bestaudio[ext=m4a]/best[height=1080]/best',
        'outtmpl': './media/downloads/%(id)s.%(ext)s',
        'merge_output_format': 'mp4'
    }) as ydl:
        info = ydl.extract_info(url, download=False)
        vid_id = info["id"]
        ext = info["ext"]
        local_path = os.path.join('./media/downloads', f"{vid_id}.{ext}")
        if not os.path.exists(local_path):
            print(f"Downloading {url} to {local_path}...")
            ydl.download([url])
        else:
            print(f"Skipping download, already exists: {local_path}")
        return local_path

def convert_multi_variant(input_file):
    vid_id = os.path.splitext(os.path.basename(input_file))[0]
    out_dir = os.path.join(HLS_DIR, vid_id)
    master_pl = os.path.join(out_dir, "master.m3u8")
    if os.path.exists(master_pl):
        print(f"Skipping HLS conversion, master playlist already exists: {master_pl}")
        return vid_id

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    print(f"Creating multi-bitrate HLS in {out_dir}...")

    # Create separate streams for each quality level
    qualities = [
        # name, scale, bitrate
        ("480p", "854:480", "800k"),
        ("720p", "1280:720", "1500k"), 
        ("1080p", "1920:1080", "3000k")
    ]

    for name, scale, bitrate in qualities:
        stream_dir = os.path.join(out_dir, name)
        Path(stream_dir).mkdir(parents=True, exist_ok=True)

        cmd = [
            "ffmpeg", "-y", "-i", input_file,
            "-vf", f"scale={scale}",
            "-c:v", "libx264", "-b:v", bitrate,
            "-preset", "fast", "-profile:v", "main",
            "-c:a", "aac", "-b:a", "128k",
            "-hls_time", "4",
            "-hls_list_size", "0",
            "-hls_segment_filename", f"{stream_dir}/segment_%03d.ts",
            f"{stream_dir}/playlist.m3u8"
        ]
        
        subprocess.run(cmd, check=True)

    # Create master playlist
    with open(master_pl, "w") as f:
        f.write("#EXTM3U\n")
        f.write("#EXT-X-VERSION:3\n")
        for name, scale, bitrate in qualities:
            res = scale.split(":")[1]
            f.write(f"#EXT-X-STREAM-INF:BANDWIDTH={bitrate[:-1]}000,RESOLUTION={scale.replace(':', 'x')}\n")
            f.write(f"{name}/playlist.m3u8\n")

    # Upload to GCS
    bucket_name = "connecthack"
    uploader = GCSUploader(bucket_name)
    
    # Upload all files in the HLS directory
    for root, dirs, files in os.walk(out_dir):
        for f in files:
            local_path = os.path.join(root, f)
            relative_path = os.path.relpath(local_path, HLS_DIR)
            uploader.upload_file(local_path, relative_path)

    return vid_id

@app.route("/hls/<path:filename>")
def serve_hls(filename):
    full_path = os.path.join(HLS_DIR, filename)
    if not os.path.isfile(full_path):
        abort(404, f"Segment/playlist not found: {filename}")
    response = make_response(send_from_directory(HLS_DIR, filename))
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return response

@app.route("/")
def index():
    videos = []
    if os.path.exists(HLS_DIR):
        videos = [d for d in os.listdir(HLS_DIR) 
                 if os.path.isdir(os.path.join(HLS_DIR, d)) and 
                 os.path.exists(os.path.join(HLS_DIR, d, "master.m3u8"))]
        videos.sort()
    return render_template('index.html', videos=videos)

@app.route("/process")
def process():
    url = request.args.get("url")
    if not url:
        return render_template('error.html', message="Missing 'url' parameter"), 400
    
    try:
        local_path = download_video(url)
        vid_id = convert_multi_variant(local_path)
        return redirect(f"/player?video={vid_id}")
    except Exception as e:
        return render_template('error.html', message=str(e)), 500

@app.route("/player")
def player():
    video_id = request.args.get("video", "")
    embed = request.args.get("embed", "0") == "1"
    
    if not video_id:
        return "<h1>No 'video' param provided</h1>", 400
        
    master_pl = os.path.join(HLS_DIR, video_id, "master.m3u8")
    if not os.path.exists(master_pl):
        return f"<h1>Video '{video_id}' not found</h1>", 404

    response = make_response(render_template('player.html', video_id=video_id, embed=embed))
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return response

@app.route("/embed-demo")
def embed_demo():
    video_id = request.args.get("video", "K4TOrB7at0Y")  # Default to K4TOrB7at0Y if no video specified
    response = make_response(render_template('embed-demo.html', video_id=video_id))
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return response

def init_db():
    # TODO: Implement proper database connection management
    #   - Use connection pooling
    #   - Implement context managers for database connections
    #   - Add error handling and connection retry logic
    #   - Consider using SQLAlchemy for better database management
    conn = sqlite3.connect('channels.db')
    c = conn.cursor()
    # TODO: Add additional fields for channel management
    #   - channel_name TEXT
    #   - subscriber_count INTEGER
    #   - webhook_url TEXT
    #   - notification_enabled BOOLEAN
    c.execute('''
        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            last_video_id TEXT,
            last_checked TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_channel_id_from_url(url):
    with YoutubeDL() as ydl:
        info = ydl.extract_info(url, download=False)
        return info.get('channel_id') or info.get('uploader_id')

def add_channel_subscription(channel_id):
    conn = sqlite3.connect('channels.db')
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO channels (channel_id, last_checked) VALUES (?, ?)',
              (channel_id, datetime.now()))
    conn.commit()
    conn.close()

def check_new_videos():
    while True:
        try:
            # TODO: Improve error handling and notification system
            #   - Add logging system
            #   - Implement retry mechanism for failed video processing
            #   - Send notifications on failures (email, webhook, etc.)
            #   - Add monitoring metrics (success rate, processing time, etc.)
            
            conn = sqlite3.connect('channels.db')
            c = conn.cursor()
            channels = c.execute('SELECT channel_id, last_video_id FROM channels').fetchall()
            
            for channel_id, last_video_id in channels:
                # TODO: Add rate limiting and quota management
                #   - Respect YouTube API quotas
                #   - Implement exponential backoff
                #   - Add delay between channel checks
                
                feed_url = f'https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}'
                feed = feedparser.parse(feed_url)
                
                if feed.entries:
                    latest_video = feed.entries[0]
                    video_id = latest_video.yt_videoid
                    
                    if video_id != last_video_id:
                        # TODO: Add webhook notifications for new videos
                        #   - Fetch webhook_url from database
                        #   - Send notification with video details
                        #   - Handle webhook failures
                        
                        video_url = f'https://www.youtube.com/watch?v={video_id}'
                        try:
                            process_video(video_url)
                            c.execute('UPDATE channels SET last_video_id = ?, last_checked = ? WHERE channel_id = ?',
                                     (video_id, datetime.now(), channel_id))
                            conn.commit()
                            print(f"Processed new video {video_id} from channel {channel_id}")
                        except Exception as e:
                            print(f"Error processing video {video_id}: {str(e)}")
            
            conn.close()
        except Exception as e:
            print(f"Error checking new videos: {str(e)}")
        
        time.sleep(300)

@app.route("/subscribe", methods=['GET', 'POST'])
def subscribe():
    # TODO: Add subscription management features
    #   - Add webhook URL configuration
    #   - Validate channel existence before subscribing
    #   - Show channel details (name, subscriber count) before confirming
    #   - Handle duplicate subscriptions gracefully
    if request.method == 'POST':
        channel_url = request.form.get('channel_url')
        if not channel_url:
            return render_template('error.html', message="Missing channel URL"), 400
        
        try:
            channel_id = get_channel_id_from_url(channel_url)
            add_channel_subscription(channel_id)
            return redirect('/')
        except Exception as e:
            return render_template('error.html', message=str(e)), 500
    
    return render_template('subscribe.html')

def main():
    # Start the background checker thread
    checker_thread = threading.Thread(target=check_new_videos, daemon=True)
    checker_thread.start()
    
    app.run(host="0.0.0.0", port=5000, debug=True)

if __name__ == "__main__":
    main()
