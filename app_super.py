
from flask import Flask, render_template, request, jsonify
import requests
import os
import re
from datetime import datetime, timedelta
from collections import Counter

app = Flask(__name__)
API_KEY = os.environ.get("YOUTUBE_API_KEY")
BASE_URL = "https://www.googleapis.com/youtube/v3"


def extract_video_id(url_or_id):
    if not url_or_id:
        return None
    value = str(url_or_id).strip()
    patterns = [
        r'(?:v=|/v/|youtu\.be/|/shorts/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$',
    ]
    for p in patterns:
        m = re.search(p, value)
        if m:
            return m.group(1)
    return value


def format_duration(duration):
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration or "")
    if not m:
        return duration or ""
    h, mi, s = m.groups()
    parts = []
    if h:
        parts.append(f"{h} saat")
    if mi:
        parts.append(f"{mi} dk")
    if s:
        parts.append(f"{s} sn")
    return " ".join(parts) or "0 sn"


def youtube_get(path, params):
    if not API_KEY:
        return {"error": {"message": "YOUTUBE_API_KEY bulunamadı"}}, 500
    params = dict(params)
    params["key"] = API_KEY
    try:
        resp = requests.get(f"{BASE_URL}/{path}", params=params, timeout=20)
        data = resp.json()
        if resp.status_code >= 400:
            return data, resp.status_code
        return data, 200
    except requests.RequestException as e:
        return {"error": {"message": str(e)}}, 500


def build_video_metrics(item):
    snippet = item["snippet"]
    stats = item.get("statistics", {})
    published = datetime.strptime(snippet["publishedAt"], "%Y-%m-%dT%H:%M:%SZ")
    hours_since = max((datetime.utcnow() - published).total_seconds() / 3600, 1)
    days_since = max(hours_since / 24, 1/24)

    view_count = int(stats.get("viewCount", 0))
    like_count = int(stats.get("likeCount", 0))
    comment_count = int(stats.get("commentCount", 0))
    views_per_hour = view_count / hours_since
    views_per_day = view_count / days_since
    engagement = ((like_count + comment_count) / max(view_count, 1)) * 100
    like_ratio = (like_count / max(view_count, 1)) * 100
    viral_score = round((views_per_hour * 0.7) + (engagement * 1000 * 0.3))

    content = item.get("contentDetails", {})
    return {
        "id": item["id"],
        "title": snippet["title"],
        "description": snippet.get("description", ""),
        "channel": snippet["channelTitle"],
        "channelId": snippet.get("channelId", ""),
        "thumbnail": snippet["thumbnails"].get("high", {}).get("url", ""),
        "publishedAt": snippet["publishedAt"],
        "hoursSince": round(hours_since, 1),
        "daysSince": round(days_since, 1),
        "duration": format_duration(content.get("duration", "")),
        "definition": content.get("definition", "").upper(),
        "viewCount": view_count,
        "likeCount": like_count,
        "commentCount": comment_count,
        "viewsPerHour": round(views_per_hour),
        "viewsPerDay": round(views_per_day),
        "engagement": round(engagement, 2),
        "likeRatio": round(like_ratio, 2),
        "viralScore": viral_score,
        "tags": snippet.get("tags", [])[:20],
        "language": snippet.get("defaultLanguage", snippet.get("defaultAudioLanguage", "Bilinmiyor")),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "apiKeyLoaded": bool(API_KEY)})


@app.route("/api/trending")
def trending():
    region = request.args.get("region", "TR")
    category = request.args.get("category", "0")
    max_results = request.args.get("limit", "20")
    params = {
        "part": "snippet,statistics,contentDetails",
        "chart": "mostPopular",
        "regionCode": region,
        "maxResults": max_results,
    }
    if category != "0":
        params["videoCategoryId"] = category

    data, status = youtube_get("videos", params)
    if status != 200:
        return jsonify({"error": data.get("error", {}).get("message", "API hatası")}), status

    videos = [build_video_metrics(item) for item in data.get("items", [])]
    videos.sort(key=lambda x: x["viralScore"], reverse=True)
    return jsonify(videos)


@app.route("/api/video/<video_id>")
def video_detail(video_id):
    data, status = youtube_get("videos", {
        "part": "snippet,statistics,contentDetails,topicDetails",
        "id": video_id,
    })
    if status != 200:
        return jsonify({"error": data.get("error", {}).get("message", "API hatası")}), status
    if not data.get("items"):
        return jsonify({"error": "Video bulunamadı"}), 404

    item = data["items"][0]
    result = build_video_metrics(item)

    ch_data, ch_status = youtube_get("channels", {
        "part": "statistics,snippet",
        "id": item["snippet"]["channelId"],
    })
    if ch_status == 200 and ch_data.get("items"):
        ch = ch_data["items"][0]
        ch_stats = ch.get("statistics", {})
        result["channelInfo"] = {
            "name": ch["snippet"]["title"],
            "subscribers": int(ch_stats.get("subscriberCount", 0)),
            "totalViews": int(ch_stats.get("viewCount", 0)),
            "videoCount": int(ch_stats.get("videoCount", 0)),
            "thumbnail": ch["snippet"]["thumbnails"]["default"]["url"],
        }
    else:
        result["channelInfo"] = {}

    return jsonify(result)


@app.route("/api/analyze", methods=["POST"])
def analyze_video():
    data = request.get_json(force=True, silent=True) or {}
    video_id = extract_video_id(data.get("url", ""))
    if not video_id:
        return jsonify({"error": "Geçersiz video URL veya ID"}), 400
    return video_detail(video_id)


@app.route("/api/search/videos")
def search_videos():
    query = request.args.get("q", "").strip()
    order = request.args.get("order", "viewCount")
    published_after = request.args.get("after", "").strip()

    if not query:
        return jsonify([])

    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "order": order,
        "maxResults": "15",
    }
    if published_after:
        days = int(published_after)
        after_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params["publishedAfter"] = after_date

    data, status = youtube_get("search", params)
    if status != 200:
        return jsonify({"error": data.get("error", {}).get("message", "Arama hatası")}), status

    video_ids = [i["id"]["videoId"] for i in data.get("items", []) if i.get("id", {}).get("videoId")]
    if not video_ids:
        return jsonify([])

    vid_data, vid_status = youtube_get("videos", {
        "part": "snippet,statistics,contentDetails",
        "id": ",".join(video_ids),
    })
    if vid_status != 200:
        return jsonify({"error": vid_data.get("error", {}).get("message", "Detay hatası")}), vid_status

    videos = [build_video_metrics(item) for item in vid_data.get("items", [])]
    videos.sort(key=lambda x: x["viralScore"], reverse=True)
    return jsonify(videos)


@app.route("/api/compare")
def compare_videos():
    ids = request.args.get("ids", "").strip()
    if not ids:
        return jsonify([])
    data, status = youtube_get("videos", {
        "part": "snippet,statistics,contentDetails",
        "id": ids,
    })
    if status != 200:
        return jsonify({"error": data.get("error", {}).get("message", "Karşılaştırma hatası")}), status
    return jsonify([build_video_metrics(item) for item in data.get("items", [])])


@app.route("/api/comments", methods=["POST"])
def comments():
    payload = request.get_json(force=True, silent=True) or {}
    video_id = extract_video_id(payload.get("url", ""))
    sort_by = payload.get("sort", "relevance")
    max_results = min(int(payload.get("limit", 100)), 100)

    if not video_id:
        return jsonify({"error": "Geçersiz video ID"}), 400

    all_comments = []
    next_page = None

    for _ in range(5):
        params = {
            "part": "snippet",
            "videoId": video_id,
            "order": sort_by,
            "maxResults": max_results,
            "textFormat": "plainText",
        }
        if next_page:
            params["pageToken"] = next_page

        data, status = youtube_get("commentThreads", params)
        if status != 200:
            return jsonify({"error": data.get("error", {}).get("message", "Yorumlar alınamadı")}), status

        for item in data.get("items", []):
            c = item["snippet"]["topLevelComment"]["snippet"]
            all_comments.append({
                "author": c["authorDisplayName"],
                "authorImage": c.get("authorProfileImageUrl", ""),
                "text": c["textDisplay"],
                "likeCount": c.get("likeCount", 0),
                "publishedAt": c["publishedAt"],
                "replyCount": item["snippet"].get("totalReplyCount", 0),
            })

        next_page = data.get("nextPageToken")
        if not next_page:
            break

    all_text = " ".join([c["text"] for c in all_comments])
    stop_words = {'bir','bu','ve','de','da','için','ile','çok','var','ben','sen','biz','siz','ama','ki','ne',
                  'the','and','is','in','to','of','a','that','it','for','you','was','on','are','this','with',
                  'be','at','have','from','or','an','not','but','what','all','were','we','when','your','can',
                  'had','i','my','me','so','if','his','her','do','has','he','she','they','been','will','no',
                  'more','ya','o','mi','mu','mı','olarak','gibi','daha','en','her','olan','kadar'}
    words = re.findall(r'\b[a-zA-ZçğıöşüÇĞİÖŞÜ]{3,}\b', all_text.lower())
    filtered = [w for w in words if w not in stop_words]
    word_freq = Counter(filtered).most_common(30)

    return jsonify({
        "total": len(all_comments),
        "comments": all_comments,
        "topLiked": sorted(all_comments, key=lambda x: x["likeCount"], reverse=True)[:10],
        "topReplied": sorted(all_comments, key=lambda x: x["replyCount"], reverse=True)[:10],
        "wordFrequency": [{"word": w, "count": c} for w, c in word_freq],
        "avgLikes": round(sum(c["likeCount"] for c in all_comments) / max(len(all_comments), 1), 1),
    })


@app.route("/api/comments/search", methods=["POST"])
def search_comments():
    payload = request.get_json(force=True, silent=True) or {}
    video_id = extract_video_id(payload.get("url", ""))
    query = payload.get("query", "").lower().strip()

    if not video_id or not query:
        return jsonify({"error": "Video ID ve arama terimi gerekli"}), 400

    all_comments = []
    next_page = None

    for _ in range(5):
        params = {
            "part": "snippet",
            "videoId": video_id,
            "maxResults": 100,
            "textFormat": "plainText",
        }
        if next_page:
            params["pageToken"] = next_page

        data, status = youtube_get("commentThreads", params)
        if status != 200:
            return jsonify({"error": data.get("error", {}).get("message", "Arama hatası")}), status

        for item in data.get("items", []):
            c = item["snippet"]["topLevelComment"]["snippet"]
            if query in c["textDisplay"].lower():
                all_comments.append({
                    "author": c["authorDisplayName"],
                    "authorImage": c.get("authorProfileImageUrl", ""),
                    "text": c["textDisplay"],
                    "likeCount": c.get("likeCount", 0),
                    "publishedAt": c["publishedAt"],
                    "replyCount": item["snippet"].get("totalReplyCount", 0),
                })

        next_page = data.get("nextPageToken")
        if not next_page:
            break

    return jsonify({"query": query, "found": len(all_comments), "comments": all_comments})


@app.route("/api/transcript", methods=["POST"])
def transcript():
    payload = request.get_json(force=True, silent=True) or {}
    video_id = extract_video_id(payload.get("url", ""))
    lang = payload.get("lang", "tr")

    if not video_id:
        return jsonify({"error": "Geçersiz video ID"}), 400

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        try:
            transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=[lang, "en"])
        except Exception:
            try:
                transcript = YouTubeTranscriptApi.get_transcript(video_id)
            except Exception:
                return jsonify({"error": "Bu video için altyazı bulunamadı", "transcript": [], "fullText": ""})

        full_text = " ".join([t["text"] for t in transcript])
        return jsonify({
            "transcript": transcript,
            "fullText": full_text,
            "wordCount": len(full_text.split()),
            "duration": round(transcript[-1]["start"] + transcript[-1]["duration"]) if transcript else 0,
        })
    except ImportError:
        return jsonify({"error": "youtube-transcript-api yüklü değil"}), 500


@app.route("/api/search/channels")
def search_channels():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"channels": []})

    data, status = youtube_get("search", {
        "part": "snippet",
        "q": query,
        "type": "channel",
        "maxResults": 10,
    })
    if status != 200:
        return jsonify({"error": data.get("error", {}).get("message", "Kanal arama hatası")}), status

    channels = []
    for item in data.get("items", []):
        snippet = item["snippet"]
        channels.append({
            "id": snippet["channelId"],
            "title": snippet["title"],
            "description": (snippet.get("description", "")[:120] + "...") if snippet.get("description") else "",
            "thumbnail": snippet["thumbnails"].get("default", {}).get("url", ""),
        })
    return jsonify({"channels": channels})


_channel_history = {}


@app.route("/api/channels/stats")
def channel_stats():
    ids = request.args.get("ids", "").strip()
    if not ids:
        return jsonify({"channels": []})

    data, status = youtube_get("channels", {
        "part": "snippet,statistics",
        "id": ids,
    })
    if status != 200:
        return jsonify({"error": data.get("error", {}).get("message", "Kanal istatistik hatası")}), status

    now_label = datetime.now().strftime("%H:%M:%S")
    channels = []
    for item in data.get("items", []):
        stats = item.get("statistics", {})
        cid = item["id"]
        snapshot = {
            "time": now_label,
            "subs": int(stats.get("subscriberCount", 0)),
            "views": int(stats.get("viewCount", 0)),
        }
        hist = _channel_history.setdefault(cid, [])
        hist.append(snapshot)
        if len(hist) > 20:
            hist[:] = hist[-20:]

        channels.append({
            "id": cid,
            "title": item["snippet"]["title"],
            "thumbnail": item["snippet"]["thumbnails"]["default"]["url"],
            "subscriberCount": snapshot["subs"],
            "viewCount": snapshot["views"],
            "videoCount": int(stats.get("videoCount", 0)),
            "history": hist,
        })

    channels.sort(key=lambda x: x["subscriberCount"], reverse=True)
    return jsonify({"channels": channels})


if __name__ == "__main__":
    if not API_KEY:
        print("YOUTUBE_API_KEY bulunamadı.")
        print("Windows CMD: set YOUTUBE_API_KEY=API_KEYIN")
    app.run(debug=True, port=5001)
