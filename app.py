import os
# Removed Gemini import
from flask import Flask, request, jsonify, render_template, send_from_directory, send_file, Response, redirect, url_for, session
import fitz  # PyMuPDF for PDF processing
from werkzeug.utils import secure_filename
import uuid
import time
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
import tempfile
import base64
from io import BytesIO
from PIL import Image, ImageDraw
import requests
import json
import shutil
import subprocess  # For LibreOffice headless PPTX -> PDF conversion
import re
from openai import OpenAI

# NVIDIA NIM API is OpenAI-compatible; the OpenAI client is reused with a custom base_url.
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
# Add imports for RAG model approach
from sentence_transformers import SentenceTransformer
import numpy as np
import faiss
from typing import List, Dict, Tuple, Optional
import torch
from tqdm import tqdm
import threading
import datetime
from collections import deque
from dotenv import load_dotenv
import socket
import platform
from shutil import which
# OAuth imports
from authlib.integrations.flask_client import OAuth
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user

# OCR is gone — NVIDIA NIM vision models transcribe slide images directly.
# LibreOffice (soffice) handles PPTX/PPT/ODP -> PDF conversion at runtime.
soffice_path = which('soffice') or which('libreoffice')
if soffice_path:
    print(f"Found LibreOffice at: {soffice_path}")
else:
    print("⚠️ LibreOffice not found in PATH — PPTX uploads will be rejected until it is installed.")

# Define TimeoutError if it doesn't exist (for Python <3.3 compatibility)
try:
    TimeoutError
except NameError:
    class TimeoutError(Exception):
        pass

# Load environment variables right after imports, before any code execution
load_dotenv()  # Take environment variables from .env file

# Rate limiting configuration for NVIDIA NIM API
# NVIDIA NIM documented public limit is 40 requests/minute; daily and token caps
# are derived conservatively from that ceiling.
RATE_LIMIT_RPM = 40       # Requests per minute (NVIDIA NIM public tier)
RATE_LIMIT_RPD = 10000    # Requests per day (conservative daily cap)
RATE_LIMIT_TPM = 40000    # Tokens per minute (~1000 tokens per request headroom)

# Rate limiting tracking
api_calls_minute = deque(maxlen=RATE_LIMIT_RPM)  # Track timestamps of calls in the last minute
api_calls_day = deque(maxlen=RATE_LIMIT_RPD)  # Track timestamps of calls in the last day
tokens_minute = deque(maxlen=RATE_LIMIT_TPM)  # Track token usage in the last minute
api_lock = threading.Lock()  # Lock for thread safety
daily_reset_time = None  # Time when daily counter was last reset

def reset_daily_counters():
    """Reset daily API counters at midnight"""
    global api_calls_day, daily_reset_time
    with api_lock:
        api_calls_day.clear()
        daily_reset_time = datetime.datetime.now().date()
        print(f"Daily API counters reset at {daily_reset_time}")

def check_rate_limits(est_tokens=200):
    """
    Check if we're within rate limits and can make an API call
    
    Args:
        est_tokens (int): Estimated tokens for this request
        
    Returns:
        tuple: (can_proceed, wait_time, reason)
    """
    global api_calls_minute, api_calls_day, tokens_minute, daily_reset_time
    
    with api_lock:
        # Check if we need to reset daily counters
        current_date = datetime.datetime.now().date()
        if daily_reset_time is None or current_date > daily_reset_time:
            reset_daily_counters()
        
        # Get current time for checking windows
        now = time.time()
        
        # Clean up expired timestamps from the minute window
        one_minute_ago = now - 60
        while api_calls_minute and api_calls_minute[0] < one_minute_ago:
            api_calls_minute.popleft()
            
        # Check if we've hit the RPM limit
        if len(api_calls_minute) >= RATE_LIMIT_RPM - 1:  # Leave 1 request buffer
            # Calculate wait time - time until oldest request expires plus a small buffer
            oldest_request_time = api_calls_minute[0]
            wait_time = max(0, oldest_request_time + 60 - now) + 0.5
            return False, wait_time, f"RPM limit reached ({RATE_LIMIT_RPM})"
        
        # Check if we've hit the RPD limit
        if len(api_calls_day) >= RATE_LIMIT_RPD - 10:  # Leave 10 request buffer
            return False, 3600, f"RPD limit reached ({RATE_LIMIT_RPD})"
            
        # Record this call preemptively
        api_calls_minute.append(now)
        api_calls_day.append(now)
        
        # We're good to proceed
        return True, 0, "ok"

def wait_for_rate_limit(est_tokens=200, max_retries=5):
    """
    Wait until we're within rate limits to make an API call
    
    Args:
        est_tokens (int): Estimated tokens for this request
        max_retries (int): Maximum number of retry attempts
        
    Returns:
        bool: Whether the call can proceed
    """
    retries = 0
    
    while retries < max_retries:
        can_proceed, wait_time, reason = check_rate_limits(est_tokens)
        
        if can_proceed:
            return True
            
        # Need to wait
        if wait_time > 0:
            print(f"Rate limit reached: {reason}. Waiting {wait_time:.1f}s before retry...")
            time.sleep(wait_time)
            retries += 1
        else:
            return True  # No wait needed
    
    # If we got here, we've retried too many times
    print(f"Rate limit retry attempts exceeded: {reason}")
    return False

def record_token_usage(prompt_tokens, completion_tokens):
    """
    Record actual token usage after an API call
    
    Args:
        prompt_tokens (int): Number of tokens in the prompt
        completion_tokens (int): Number of tokens in the completion
    """
    global tokens_minute
    
    with api_lock:
        # Record token usage for this minute
        tokens_minute.append(prompt_tokens + completion_tokens)

# Configure NVIDIA NIM API for models
groq_api_key = os.environ.get("NVIDIA_API_KEY")
if not groq_api_key:
    print("⚠️ WARNING: No NVIDIA API key found in environment variables")
    print("Set your NVIDIA_API_KEY environment variable for AI functionality to work")
    groq_api_key = ""  # Empty string instead of hardcoded key

# Initialize NVIDIA NIM client - will be initialized properly when API key is available
client = None
if groq_api_key:
    client = OpenAI(api_key=groq_api_key, base_url=NVIDIA_BASE_URL)
    print("✅ NVIDIA NIM client initialized successfully")

# Model configuration - use NVIDIA-hosted Gemma model
groq_model = "google/gemma-4-31b-it"  # The NVIDIA NIM model name

# Variable to track if NVIDIA NIM is available (keeping the legacy name to
# avoid touching every call site — it's just a module-local flag now).
groq_available = True

# Vision + batching configuration for the NVIDIA NIM pipeline.
VISION_MODEL = "google/gemma-4-31b-it"  # Same NIM endpoint; it handles text+image messages.
PROCESSING_BATCH_SIZE = 5                # Slides processed per batch.
# A batch of 5 concurrent calls takes ~30-60s for vision, so we are already
# well under 40 RPM without any pause. 2s is just a courtesy gap to avoid
# thundering-herd bursts on the NIM edge.
PROCESSING_BATCH_DELAY_SECONDS = 2.0
RETRY_BATCH_SIZE = 5                     # Retries are capped at 5 slides per group.
MAX_RETRIES_PER_SLIDE = 2                # Each failed slide gets up to 2 retries.
RETRY_BACKOFF_SECONDS = 6.0              # Wait between retry rounds so transient NIM
                                         # rate-limit/capacity errors get a chance to clear.

# Global dictionaries for storage
slide_contents = {}  # Store slide content by session ID
slide_contents_structured = {}  # Store structured slide content by session ID
slide_images = {}  # Store image paths by session ID
session_images = {}  # Store image paths for each session
chat_sessions = {}  # Store chat history by session ID
summary_cache = {}  # Cache for summaries to avoid redundant API calls

# Per-session, per-slide processing state. Shape:
#   slide_status[session_id] = {
#       "overall": "uploading"|"transcribing"|"summarizing"|"retrying"|"complete"|"failed",
#       "total_slides": int,
#       "slides": {
#           "1": {"transcription": "pending"|"done"|"failed",
#                 "summary":       "pending"|"done"|"failed",
#                 "retries": int,
#                 "error": str|None},
#           ...
#       }
#   }
# The frontend polls /processing_status/<session_id> and animates thumbnails
# off this dict. It is a plain Python dict guarded by status_lock.
slide_status = {}
status_lock = threading.Lock()

def _init_session_status(session_id, total_slides):
    with status_lock:
        slide_status[session_id] = {
            "overall": "uploading",
            "total_slides": total_slides,
            "slides": {
                str(n): {"transcription": "pending", "summary": "pending",
                         "retries": 0, "error": None}
                for n in range(1, total_slides + 1)
            },
        }

def _update_slide_state(session_id, slide_num, **fields):
    with status_lock:
        sess = slide_status.get(session_id)
        if not sess:
            return
        slot = sess["slides"].get(str(slide_num))
        if slot is None:
            return
        slot.update(fields)

def _set_overall_state(session_id, overall):
    with status_lock:
        sess = slide_status.get(session_id)
        if sess:
            sess["overall"] = overall


def _get_slide_state(session_id, slide_num):
    """Snapshot a single slide's status entry (or None)."""
    with status_lock:
        sess = slide_status.get(session_id)
        if not sess:
            return None
        slot = sess.get("slides", {}).get(str(slide_num))
        return dict(slot) if slot else None


def wait_for_slide_ready(session_id, slide_num, max_wait_seconds=45.0, poll_interval=0.5):
    """Poll slide_status until this slide's transcription is 'done'.

    Yields dicts of the form {"progress": "Transcribing slide N..."} at every
    poll tick so callers can forward them as SSE progress events. Terminates
    with one of:
        {"ready": True}             — transcription is done; caller may proceed.
        {"ready": False, "reason": "failed"|"timeout"|"no_session"}

    Callers that don't need progress events can just drain the generator and
    look at the final dict.
    """
    deadline = time.time() + max_wait_seconds
    last_label = None
    while True:
        state = _get_slide_state(session_id, slide_num)
        if state is None:
            yield {"ready": False, "reason": "no_session"}
            return

        tstate = state.get("transcription")
        if tstate == "done":
            yield {"ready": True}
            return
        if tstate == "failed":
            # Only give up if no retries are in flight. The pipeline will flip
            # it back to "processing" when retrying.
            retries = state.get("retries", 0) or 0
            if retries >= MAX_RETRIES_PER_SLIDE:
                yield {"ready": False, "reason": "failed",
                       "error": state.get("error")}
                return

        if time.time() >= deadline:
            yield {"ready": False, "reason": "timeout"}
            return

        label = f"Transcribing slide {slide_num}..."
        if tstate == "processing":
            label = f"Transcribing slide {slide_num}..."
        elif (state.get("retries") or 0) > 0:
            label = f"Retrying slide {slide_num} (attempt {state['retries'] + 1})..."
        if label != last_label:
            yield {"progress": label}
            last_label = label

        time.sleep(poll_interval)


def build_neighbor_context_from_texts(slide_texts, slide_num, radius=2, max_chars=500):
    """Same shape as _collect_neighbor_context but takes a slide_texts dict
    (used by interactive endpoints that don't have the structured dict in hand)."""
    parts = []
    total = len(slide_texts)
    for offset in range(-radius, radius + 1):
        if offset == 0:
            continue
        n = slide_num + offset
        if n < 1 or n > total:
            continue
        text = (slide_texts.get(str(n)) or "").strip()
        if not text:
            continue
        parts.append(f"Slide {n}:\n{text[:max_chars].strip()}")
    return "\n\n".join(parts)


# ---------- PPTX -> PDF conversion ----------

SUPPORTED_UPLOAD_EXTENSIONS = {".pdf", ".pptx", ".ppt"}

def convert_to_pdf_if_needed(source_path, work_dir):
    """If source is PPTX/PPT, convert to PDF via headless LibreOffice and return
    the new path. If it's already a PDF, return it unchanged. Raises on failure."""
    ext = os.path.splitext(source_path)[1].lower()
    if ext == ".pdf":
        return source_path
    if ext not in SUPPORTED_UPLOAD_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}")

    soffice = which("soffice") or which("libreoffice")
    if not soffice:
        raise RuntimeError(
            "LibreOffice (soffice) is not installed — cannot convert PPTX/PPT. "
            "Install libreoffice-impress in the container."
        )

    os.makedirs(work_dir, exist_ok=True)
    print(f"Converting {source_path} -> PDF via {soffice}")
    result = subprocess.run(
        [soffice, "--headless", "--norestore", "--nologo", "--nofirststartwizard",
         "--convert-to", "pdf", "--outdir", work_dir, source_path],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice conversion failed: {result.stderr or result.stdout}")

    base = os.path.splitext(os.path.basename(source_path))[0]
    pdf_path = os.path.join(work_dir, f"{base}.pdf")
    if not os.path.isfile(pdf_path):
        raise RuntimeError(f"LibreOffice reported success but no PDF at {pdf_path}")
    return pdf_path


# ---------- Vision transcription ----------

def _encode_image_data_url(img_path):
    with open(img_path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")

VISION_TRANSCRIBE_SYSTEM = (
    "You convert presentation slide images into faithful text transcriptions "
    "used for downstream retrieval and summarization."
)

VISION_TRANSCRIBE_USER = (
    "Transcribe this slide image into plain text.\n"
    "Rules:\n"
    "1. Copy all visible text verbatim, preserving hierarchy (title, subtitle, bullets, sub-bullets, captions).\n"
    "2. For charts, diagrams, tables, or images, describe them factually in one short paragraph: "
    "what is being shown, the axes/columns/labels, the relationships, and any concrete values.\n"
    "3. Do NOT add commentary, interpretation, or 'this slide shows'. Output only the transcription.\n"
    "4. If the slide is almost blank (e.g. a divider), output a single short descriptive line."
)

def vision_transcribe_slide(img_path, slide_num, timeout=90.0):
    """Transcribe a slide image via NVIDIA NIM vision. Returns transcription string.
    Raises on failure — the caller decides whether to retry."""
    api_key = os.environ.get("NVIDIA_API_KEY", "")
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY not set")

    vision_client = OpenAI(api_key=api_key, base_url=NVIDIA_BASE_URL)
    data_url = _encode_image_data_url(img_path)

    completion = vision_client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {"role": "system", "content": VISION_TRANSCRIBE_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": VISION_TRANSCRIBE_USER},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
        temperature=0.1,
        max_tokens=1200,
        timeout=timeout,
    )

    message = completion.choices[0].message
    text = getattr(message, "content", None) or getattr(message, "reasoning_content", "")
    text = (text or "").strip()
    if not text:
        raise RuntimeError(f"Empty transcription for slide {slide_num}")
    return text

# RAG Model Configuration
# Using all-MiniLM-L6-v2 as a lightweight, quantizable embedding model that works well for RAG
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
EMBEDDING_DIMENSION = 384  # Dimension for all-MiniLM-L6-v2
embedding_model = None
use_half_precision = True  # Set to True to use FP16 precision (saves memory)

# Dictionary to store FAISS indices for each session
faiss_indices = {}
# Dictionary to store slide chunks for each session
slide_chunks = {}
# Dictionary to store slide mapping (chunk_id -> slide_num) for each session
chunk_to_slide_map = {}

def load_embedding_model():
    """Load the embedding model once and keep it in memory"""
    global embedding_model
    
    if embedding_model is None:
        print(f"Loading embedding model: {EMBEDDING_MODEL}")
        try:
            # Load the model with half precision if available (saves memory)
            embedding_model = SentenceTransformer(EMBEDDING_MODEL)
            if use_half_precision and torch.cuda.is_available():
                embedding_model.half()  # Convert to FP16
                print("Using half precision (FP16) for embedding model")
            elif torch.cuda.is_available():
                print("Using GPU for embedding model")
                embedding_model.to('cuda')
            else:
                print("Using CPU for embedding model")
                
        except Exception as e:
            print(f"Error loading embedding model: {str(e)}")
            return False
            
    return True

def chunk_text(text: str, chunk_size: int = 150, overlap: int = 30) -> List[str]:
    """Split text into overlapping chunks for better retrieval"""
    words = text.split()
    chunks = []
    
    if len(words) <= chunk_size:
        return [text]
        
    for i in range(0, len(words), chunk_size - overlap):
        chunk = " ".join(words[i:i + chunk_size])
        chunks.append(chunk)
        if i + chunk_size >= len(words):
            break
            
    return chunks

def create_slide_embeddings(session_id: str, slide_texts: Dict[str, str]):
    """Create embeddings for slides and build a FAISS index"""
    global faiss_indices, slide_chunks, chunk_to_slide_map
    
    if not load_embedding_model():
        print("Failed to load embedding model, skipping embeddings creation")
        return False
        
    all_chunks = []
    slide_map = {}
    chunk_idx = 0
    
    print(f"Creating embeddings for {len(slide_texts)} slides in session {session_id}")
    
    # For memory and token efficiency, limit the number of chunks
    max_chunks_per_slide = 3  # Limit chunks per slide
    total_chunks_limit = 300  # Overall chunk limit to prevent excessive processing
    
    # Process each slide text into chunks
    for slide_num, text in tqdm(slide_texts.items(), desc="Processing slides"):
        # Convert slide_num to integer for internal use if needed
        int_slide_num = int(slide_num)
        
        # Skip very short texts (likely image-only slides)
        if len(text.split()) < 20:
            # Create just one chunk for short texts
            all_chunks.append(text)
            slide_map[chunk_idx] = int_slide_num
            chunk_idx += 1
            continue
        
        # Create chunks from the slide text
        slide_chunks_result = chunk_text(text)
        
        # Take only the most important chunks (beginning, middle, end)
        # if we have too many chunks for this slide
        if len(slide_chunks_result) > max_chunks_per_slide:
            # Keep first, last, and middle chunk for each slide
            selected_chunks = []
            selected_chunks.append(slide_chunks_result[0])  # First chunk
            
            if len(slide_chunks_result) > 2:
                middle_idx = len(slide_chunks_result) // 2
                selected_chunks.append(slide_chunks_result[middle_idx])  # Middle chunk
                
            selected_chunks.append(slide_chunks_result[-1])  # Last chunk
            slide_chunks_result = selected_chunks
        
        # Add chunks to our collection
        for chunk in slide_chunks_result:
            all_chunks.append(chunk)
            # Map this chunk index back to its slide number
            slide_map[chunk_idx] = int_slide_num
            chunk_idx += 1
            
            # Check if we've hit the overall limit
            if len(all_chunks) >= total_chunks_limit:
                print(f"Hit chunk limit of {total_chunks_limit}, stopping chunk creation")
                break
                
        # Check again after processing a full slide
        if len(all_chunks) >= total_chunks_limit:
            break
    
    # Check if we have any chunks
    if not all_chunks:
        print("No chunks created, skipping embeddings")
        return False
        
    # Store slide chunks and mapping
    slide_chunks[session_id] = all_chunks
    chunk_to_slide_map[session_id] = slide_map
    
    # Now create embeddings and build the FAISS index
    print(f"Generating embeddings for {len(all_chunks)} chunks in batches of 50")
    
    # Process in batches to avoid memory issues
    batch_size = 50
    all_embeddings = []
    
    for i in range(0, len(all_chunks), batch_size):
        end_idx = min(i + batch_size, len(all_chunks))
        batch = all_chunks[i:end_idx]
        print(f"Processing batch {i//batch_size + 1}/{(len(all_chunks)-1)//batch_size + 1}")
        
        # Generate embeddings for this batch
        try:
            with torch.no_grad():
                batch_embeddings = embedding_model.encode(batch)
            all_embeddings.extend(batch_embeddings)
        except Exception as e:
            print(f"Error generating embeddings for batch: {str(e)}")
            continue
    
    # Create FAISS index
    try:
        dimension = len(all_embeddings[0])
        index = faiss.IndexFlatL2(dimension)
        index.add(np.array(all_embeddings).astype('float32'))
        
        # Store the index
        faiss_indices[session_id] = index
        
        print(f"Successfully created embeddings and FAISS index for session {session_id}")
        return True
    except Exception as e:
        print(f"Error creating FAISS index: {str(e)}")
        return False

def retrieve_relevant_chunks(session_id: str, query: str, top_k: int = 3) -> List[Tuple[str, int, float]]:
    """Retrieve most relevant chunks for a query using vector similarity search"""
    if session_id not in faiss_indices or not load_embedding_model():
        print(f"No embeddings found for session {session_id} or model loading failed")
        return []
        
    try:
        # Generate embedding for the query
        query_embedding = embedding_model.encode([query], convert_to_numpy=True)
        
        # Normalize for cosine similarity
        faiss.normalize_L2(query_embedding)
        
        # Search the index
        index = faiss_indices[session_id]
        scores, indices = index.search(query_embedding, top_k)
        
        # Get the corresponding chunks and slide numbers
        results = []
        for score, idx in zip(scores[0], indices[0]):
            # Ensure idx is an integer
            idx = int(idx)
            if idx >= 0 and idx < len(slide_chunks[session_id]):
                chunk = slide_chunks[session_id][idx]
                # Ensure consistent integer key usage for slide number lookup
                slide_num = chunk_to_slide_map[session_id].get(idx)
                # Convert slide_num to int if it exists
                if slide_num is not None:
                    slide_num = int(slide_num)
                    
                # Add to results only if score is above minimum threshold
                if float(score) > 0.6:  # Increased threshold for higher relevance
                    results.append((chunk, slide_num, float(score)))
                
        return results
        
    except Exception as e:
        print(f"Error retrieving chunks: {e}")
        return []

def get_context_for_query(session_id: str, query: str, current_slide: Optional[int] = None) -> Tuple[str, List[int]]:
    """Get the most relevant context for a query, with a bias toward the current slide"""
    # Check for slide references in the query
    slide_query_match = re.search(r'(?:explain|show|tell me about|what is in|describe|summarize|content of)\s+slide\s+(\d+)(?:\s+|$|\?)', query.lower())
    
    # If query is about a specific slide but current_slide isn't set, update it
    if slide_query_match and not current_slide:
        try:
            current_slide = int(slide_query_match.group(1))
            print(f"Detected slide reference in query, setting current_slide to {current_slide}")
        except ValueError:
            pass
            
    # Weight current slide more heavily if provided
    if current_slide is not None:
        # Check if slide exists
        slide_data = app.config.get('SLIDE_DATA', {}).get(session_id, {})
        extraction_data = slide_data.get('extraction_data', {})
        slide_texts = extraction_data.get('slide_texts', {})
        
        if str(current_slide) in slide_texts:
            # This is a valid slide - prioritize it
            print(f"Prioritizing slide {current_slide} in context retrieval")
            # Combine the query with the slide number to bias the search
            biased_query = f"Slide {current_slide}: {query}"
            chunks = retrieve_relevant_chunks(session_id, biased_query, top_k=5)
            
            # If chunks were found, ensure the current slide is included
            current_slide_chunk = None
            slides_found = set()
            
            for chunk, slide_num, score in chunks:
                slides_found.add(slide_num)
                
            # If current slide isn't in the results, force include it
            if current_slide not in slides_found:
                # Get the content for the current slide
                str_slide_content, exists = get_slide_content(session_id, current_slide)
                if exists:
                    # Create a special chunk entry for this slide
                    chunks.insert(0, (str_slide_content, current_slide, 1.0))  # Add at the beginning with max score
        else:
            # Slide doesn't exist, use regular RAG
            chunks = retrieve_relevant_chunks(session_id, query, top_k=5)
    else:
        chunks = retrieve_relevant_chunks(session_id, query, top_k=5)
        
    if not chunks:
        return "", []
        
    # Combine chunks into context
    context_parts = []
    slide_nums = []
    
    # Track total context size to avoid excessive token usage
    max_context_size = 2000  # Limiting total context to ~500 tokens
    current_context_size = 0
    
    for chunk, slide_num, score in chunks:
        if score < 0.6:  # Increased threshold for relevance
            continue
            
        # Calculate the size of this chunk
        chunk_size = len(chunk)
        
        # If adding this chunk would exceed our limit, skip it
        if current_context_size + chunk_size > max_context_size:
            # If this is the first chunk, truncate it instead of skipping
            if not context_parts:
                truncated_chunk = chunk[:max_context_size]
                context_parts.append(f"Slide {slide_num}: {truncated_chunk}")
                slide_nums.append(slide_num)
            break
            
        # Add this chunk to our context
        context_parts.append(f"Slide {slide_num}: {chunk}")
        slide_nums.append(slide_num)
        current_context_size += chunk_size
        
    context = "\n\n".join(context_parts)
    return context, slide_nums

# Function to check if Groq is available
def check_groq_availability():
    """Test if Groq API is available and set the global flag accordingly"""
    global groq_available
    
    # Always assume the API is available to prevent startup issues
    print("✅ Groq API check bypassed - assuming API is available")
    groq_available = True
    return True

# Check Groq API availability on startup
print("\n" + "="*50)
print("STUDYMATE INITIALIZATION")
print("="*50)
print("Checking Groq Llama 3.1 8B model availability...")
# Bypassing API check to prevent startup hangs - Assuming API is available
groq_available = True
print("\n✅ Groq Llama 3.1 8B model check bypassed")
print("StudyMate will use the Llama 3.1 8B model for all AI operations.")
print("API will be checked on first actual use.")
print("="*50 + "\n")

# Create the Flask app
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'slides'
app.config['IMAGE_FOLDER'] = 'static/slide_images'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB limit
app.config['SLIDE_DATA'] = {}  # Initialize empty slide data dictionary
# Add secret key for sessions
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(24))

# Configure session cookie for HTTPS in production
if os.environ.get('PRODUCTION', 'False').lower() == 'true' or 'K_SERVICE' in os.environ:
    app.config['SESSION_COOKIE_SECURE'] = True
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Ensure upload and image folders exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['IMAGE_FOLDER'], exist_ok=True)

# Create placeholder image if it doesn't exist
placeholder_path = os.path.join(app.root_path, 'static', 'images', 'placeholder.png')
if not os.path.exists(placeholder_path):
    print(f"Creating placeholder image at {placeholder_path}")
    try:
        # Create a simple placeholder image
        img = Image.new('RGB', (800, 600), color=(240, 240, 240))
        draw = ImageDraw.Draw(img)
        draw.text((400, 300), "Image not available", fill=(100, 100, 100))
        img.save(placeholder_path)
    except Exception as e:
        print(f"Error creating placeholder image: {str(e)}")

# Load environment variables for OAuth
client_id = os.environ.get('GOOGLE_CLIENT_ID')
client_secret = os.environ.get('GOOGLE_CLIENT_SECRET')

# Determine appropriate redirect URI based on environment
is_production = os.environ.get('PRODUCTION', 'False').lower() == 'true'
is_cloud_run = 'K_SERVICE' in os.environ  # Check if running on Cloud Run
is_gae = 'GAE_SKIP_GCS_INIT' in os.environ  # Check if running on App Engine

# Get the base URL from environment variable, with fallback logic
base_url = os.environ.get('APP_BASE_URL')

print("\n=== OAuth Configuration ===")
print(f"Environment Variables:")
print(f"PRODUCTION: {os.environ.get('PRODUCTION', 'Not Set')}")
print(f"K_SERVICE: {os.environ.get('K_SERVICE', 'Not Set')}")
print(f"GAE_SKIP_GCS_INIT: {os.environ.get('GAE_SKIP_GCS_INIT', 'Not Set')}")
print(f"APP_BASE_URL: {os.environ.get('APP_BASE_URL', 'Not Set')}")
print(f"\nEnvironment Detection:")
print(f"is_production: {is_production}")
print(f"is_cloud_run: {is_cloud_run}")
print(f"is_gae: {is_gae}")

# If base_url is not set in environment, determine it based on environment
if not base_url:
    if is_cloud_run or is_gae:
        base_url = 'https://sumora.kauzway.com'
        print(f"\nRunning on Cloud Platform, using production URL: {base_url}")
    else:
        base_url = 'http://localhost:5002'
        print(f"\nRunning locally, using development URL: {base_url}")

redirect_uri = f"{base_url}/auth/google/callback"
print(f"\nFinal Configuration:")
print(f"Base URL: {base_url}")
print(f"Redirect URI: {redirect_uri}")
print("========================\n")

# Setup OAuth
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=client_id,
    client_secret=client_secret,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={
        'scope': 'openid email profile',
        'prompt': 'select_account'
    }
)

# Setup Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# User model for Flask-Login
class User(UserMixin):
    def __init__(self, id, email, name):
        self.id = id
        self.email = email
        self.name = name

# Flask-Login user loader
@login_manager.user_loader
def load_user(user_id):
    # Simple user storage - in production, you'd use a database
    if 'user_data' in session and session['user_data'].get('id') == user_id:
        data = session['user_data']
        return User(data['id'], data['email'], data['name'])
    return None

# Global variables
active_session_id = None

def extract_text_from_pdf(pdf_path, session_id):
    """Render every PDF page to a PNG for vision processing.

    This used to also run fitz.get_text() and OCR; that pipeline has been
    replaced by NVIDIA NIM vision transcription, which runs asynchronously
    in the background. Here we only produce the images and register them.

    Returns (image_paths, total_slides).
    """
    document = fitz.open(pdf_path)
    print(f"Opened PDF: {pdf_path} with {len(document)} pages")

    image_paths = []
    if session_id not in session_images:
        session_images[session_id] = []

    for i, page in enumerate(document):
        slide_num = i + 1
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        img_data = pix.tobytes("png")
        img_path = tempfile.mktemp(suffix='.png',
                                    prefix=f'{session_id}_slide{slide_num}_')
        with open(img_path, 'wb') as img_file:
            img_file.write(img_data)
        image_paths.append(img_path)
        session_images[session_id].append(img_path)

    document.close()

    # Seed placeholders so any code that reads these dicts before transcription
    # lands doesn't explode.
    slide_contents_structured[session_id] = {
        str(i + 1): "" for i in range(len(image_paths))
    }
    slide_contents[session_id] = ""
    return image_paths, len(image_paths)

def get_or_create_chat_session(session_id):
    """Get an existing chat session or create a new one"""
    # This function is no longer used since we've removed Gemini/Google models
    # Kept as a placeholder in case code references it
    return None

# Function to check if a slide is likely just a title slide
def is_title_slide(slide_text):
    """Determine if a slide is likely just a title or author slide"""
    # Remove "Slide X:" prefix if present
    if "Slide " in slide_text and ":" in slide_text:
        parts = slide_text.split(":", 1)
        if len(parts) > 1:
            content = parts[1].strip()
        else:
            content = slide_text
    else:
        content = slide_text
        
    # Count words after removing the prefix
    word_count = len(content.split())
    
    # Typical patterns for title slides
    title_patterns = [
        "agenda", "overview", "introduction", "thank you", "questions",
        "presented by", "author", "title", "contents", "outline"
    ]
    
    # Check if the content is very short (definitely a title)
    is_title = (word_count < 10)  # If fewer than 10 words, likely a title slide
    
    # Check for common title slide patterns only for short texts
    if word_count < 25:  # Be more selective about pattern matching for longer content
        for pattern in title_patterns:
            if pattern.lower() in content.lower():
                is_title = True
                break
            
    return is_title

SUMMARY_SYSTEM_PROMPT = (
    "You are an expert analyst distilling presentation slides into concept-dense "
    "summaries. Your summaries should let a reader grasp the slide's core idea "
    "in under 15 seconds — no filler, no restating obvious context.\n\n"
    "REQUIRED STRUCTURE (markdown):\n"
    "1. **One-line thesis** in bold — what the slide actually argues, teaches, or claims. "
    "Never start with 'This slide', 'The slide', 'In this slide', or similar filler.\n"
    "2. Two to three short bullet points with the specific facts, numbers, mechanisms, "
    "or steps that support the thesis. Prefer concrete values over paraphrase.\n"
    "3. OPTIONAL final line, only when there is a substantive, evident link — "
    "\"Connects to: Slide N (<one-clause reason>).\" "
    "Include it only if the named slide directly defines, extends, contradicts, "
    "or is continued by this one. Never invent a link. Omit entirely otherwise.\n\n"
    "HARD RULES:\n"
    "- Do not invent information that isn't in the transcription.\n"
    "- Total length 60–90 words.\n"
    "- Use bold only for the thesis and at most three key terms."
)

def generate_groq_summary(slide_text, slide_num, streaming=True,
                          neighbor_context="", total_slides=0):
    """Generate a concept-dense slide summary via NVIDIA NIM.

    In streaming mode (used by /stream_summary) this returns a generator and
    recovers gracefully on errors. In non-streaming mode (used by the batch
    pipeline) it raises exceptions so the retry loop can catch and regroup
    failed slides.
    """
    api_key = os.environ.get("NVIDIA_API_KEY", "")
    if not api_key:
        if streaming:
            return iter([generate_basic_summary(slide_text, slide_num)])
        raise RuntimeError("NVIDIA_API_KEY not set")

    # IMPORTANT: do NOT set socket.setdefaulttimeout here. It is a process-wide
    # default and was previously silently killing every in-flight NIM call
    # happening on other threads (batch transcriptions, chat, etc.). The
    # per-call `timeout=` on the OpenAI client is the right knob.
    client_local = OpenAI(api_key=api_key, base_url=NVIDIA_BASE_URL)

    context_block = ""
    if neighbor_context:
        context_block = (
            "Context from adjacent slides (for optional cross-reference only — "
            "do not summarize these):\n" + neighbor_context + "\n\n"
        )
    total_hint = f"Deck size: {total_slides} slides. " if total_slides else ""
    user_message = (
        f"{total_hint}Produce the summary for Slide {slide_num}.\n\n"
        f"{context_block}"
        f"Slide {slide_num} transcription:\n{slide_text}"
    )
    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    if streaming:
        print(f"NIM streaming summary call for slide {slide_num}")
        try:
            stream = client_local.chat.completions.create(
                model=VISION_MODEL,
                messages=messages,
                temperature=0.3,
                max_tokens=400,
                stream=True,
                timeout=90.0,
            )

            def process_stream():
                try:
                    start_time = time.time()
                    timeout_seconds = 90.0
                    for chunk in stream:
                        if time.time() - start_time > timeout_seconds:
                            yield "<<<TIMEOUT_ERROR>>>"
                            break
                        if not getattr(chunk, "choices", None):
                            continue
                        choice = chunk.choices[0]
                        delta = getattr(choice, "delta", None)
                        if delta is None:
                            continue
                        content = (getattr(delta, "content", None)
                                   or getattr(delta, "reasoning_content", None))
                        if content:
                            yield content
                except Exception as stream_error:
                    print(f"Error during streaming: {stream_error}")
                    yield f"Error: {str(stream_error)}"

            return process_stream()
        except Exception as stream_error:
            print(f"Streaming setup failed: {stream_error}; falling back to non-streaming")
            # fall through to non-streaming

    response = client_local.chat.completions.create(
        model=VISION_MODEL,
        messages=messages,
        temperature=0.3,
        max_tokens=400,
        timeout=90.0,
    )
    message = response.choices[0].message
    summary = (getattr(message, "content", None)
               or getattr(message, "reasoning_content", "")).strip()
    if not summary:
        raise RuntimeError(f"Empty summary from NIM for slide {slide_num}")
    return summary


# Function to generate a more meaningful summary when API models are unavailable
def generate_basic_summary(slide_text, slide_num):
    """
    Generate a basic summary locally without API calls.
    This is a fallback method when API-based generation fails.
    
    Args:
        slide_text: The text content of the slide
        slide_num: The slide number
        
    Returns:
        str: A simple summary of the slide content
    """
    print(f"Generating basic summary locally for slide {slide_num}")
    text = slide_text.strip()

    if len(text) < 100:
        return f"Slide {slide_num} contains: {text}"

    sentences = [s.strip() for s in text.replace('\n', ' ').split('.') if s.strip()]
    if not sentences:
        return f"Slide {slide_num} contains text that could not be summarized: {text[:100]}..."

    picks = [sentences[0]]
    if len(sentences) > 2:
        picks.append(sentences[len(sentences) // 2])
    if len(sentences) > 1:
        picks.append(sentences[-1])
    return ". ".join(picks) + "."

# Function to extract title from slide text
def extract_slide_title(slide_text, slide_num):
    """
    Extract a reasonable title from slide text.
    
    Args:
        slide_text (str): The text content of the slide
        slide_num (int): The slide number
        
    Returns:
        str: The extracted title or a default title
    """
    # Default title
    default_title = f"Slide {slide_num}"
    
    if not slide_text or len(slide_text.strip()) == 0:
        return default_title
    
    # Split the text into lines
    lines = slide_text.strip().split("\n")
    
    # Find the first non-empty line with reasonable length for a title
    for line in lines:
        line = line.strip()
        if line and len(line) > 0:
            # Title should not be extremely long
            words = line.split()
            if 1 <= len(words) <= 10:
                return line
            elif len(words) > 10:
                # If first line is too long, use a shortened version
                return " ".join(words[:8]) + "..."
            
    # If we didn't find a good title, return the default
    return default_title

def get_slide_content(session_id, slide_num, wait_if_pending=True, max_wait_seconds=20.0):
    """Get the transcribed content of a specific slide.

    If the slide is a valid index but its transcription hasn't landed yet, this
    briefly waits for the background pipeline rather than returning False (which
    would make chat incorrectly tell the user the slide doesn't exist).

    Returns (slide_text, exists) — `exists` reflects whether the slide is a
    valid position in the deck, not whether text is available right now.
    """
    try:
        str_slide_num = str(slide_num)
        slide_data = app.config.get('SLIDE_DATA', {}).get(session_id, {})
        if not slide_data:
            print(f"No data found for session {session_id}")
            return "", False

        extraction_data = slide_data.get('extraction_data', {})
        if not extraction_data:
            return "", False

        slide_texts = extraction_data.get('slide_texts', {})
        if not slide_texts:
            return "", False

        if str_slide_num not in slide_texts:
            total_slides = len(slide_texts)
            print(f"Slide {slide_num} not found. Available slides: 1-{total_slides}")
            return "", False

        text = slide_texts.get(str_slide_num, "")
        if text.strip() or not wait_if_pending:
            return text, True

        # Slide exists but transcription is still in flight — poll briefly.
        print(f"Chat waiting on transcription of slide {slide_num}")
        for event in wait_for_slide_ready(session_id, slide_num,
                                          max_wait_seconds=max_wait_seconds):
            if event.get("ready"):
                return slide_texts.get(str_slide_num, ""), True
            if "progress" in event:
                continue
            # Not ready, not progress -> failure/timeout/no_session.
            return "", True   # slide EXISTS (valid index), just no text yet.
        return slide_texts.get(str_slide_num, ""), True

    except Exception as e:
        print(f"Error retrieving slide {slide_num}: {str(e)}")
        return "", False

# Function to generate chat responses using Groq's QWQ 32B model with RAG context
def generate_groq_chat_response(user_message, session_id=None, current_slide=None):
    """Generate a chat response using RAG context with Groq's Llama 3.1 8B model"""
    
    try:
        print(f"\n=== Starting Groq chat response generation ===")
        
        # Use active session if none provided
        if not session_id:
            session_id = active_session_id
        
        # Create messages list for the chat
        messages = [
            {"role": "system", "content": """You are a presentation analyst helping the user deeply understand a specific deck. The retrieved slide content in subsequent system messages is your ONLY source of truth — never invent facts that aren't there.

HOW TO ANSWER
1. Give only the direct answer. No "let me think", "looking at the slides", "based on the content" preambles. No meta-commentary about your process.
2. When the answer draws from more than one slide, cite each inline as "(Slide N)" at the end of the relevant sentence, and add ONE short clause explaining how the slides relate to each other. Example: "Revenue model charges a flat ₹50 per booking (Slide 6), which builds on the pricing principles introduced earlier (Slide 3)."
3. When a concept from one slide is clarified, defined, extended, or contradicted by another, say so explicitly. Example: "Slide 4 introduces the metric; Slide 7 shows how it's computed."
4. If the user asks about a slide that doesn't exist, say so clearly, then point to the closest related slides.
5. Match the length of your answer to the question: short question -> short answer, complex question -> structured answer with bullets.
6. Prefer concrete specifics (numbers, names, mechanisms) from the slides over vague summaries.
7. For code or technical content, explain the purpose, key components, and interactions — using actual snippets or values from the slides when present."""}
        ]
        
        # Handle slide-specific queries
        slide_query_match = re.search(r'(?:explain|show|tell me about|what is in|describe|summarize|content of)\s+slide\s+(\d+)(?:\s+|$|\?)', user_message.lower())
        specific_slide = None
        slide_content = ""
        slide_exists = False
        
        if slide_query_match:
            specific_slide = int(slide_query_match.group(1))
            print(f"Detected request for specific slide: {specific_slide}")

            slide_content, slide_exists = get_slide_content(session_id, specific_slide)

            if slide_exists and slide_content.strip():
                context_message = f"The user is asking about Slide {specific_slide}. Here is the content of that slide:\n\n{slide_content}"
                messages.append({"role": "system", "content": context_message})
                current_slide = specific_slide
            elif slide_exists and not slide_content.strip():
                # Slide is a valid position but transcription hasn't completed.
                # Return early — no point invoking the LLM for content we don't have.
                return (
                    f"Slide {specific_slide} is still being processed. Please wait a moment "
                    f"for transcription to finish, then ask again — it should only take a few seconds."
                )
            else:
                messages.append({"role": "system", "content": f"The user asked about Slide {specific_slide}, but this slide doesn't appear to exist in the current presentation."})
        
        # Get RAG context for the user question
        context = ""
        relevant_slides = []
        
        if session_id and session_id in faiss_indices:
            context, relevant_slides = get_context_for_query(session_id, user_message, current_slide)
            
        if context and not (specific_slide and slide_exists):
            # Only add RAG context if we didn't already add the specific slide content
            context_message = f"Here is the relevant content from the presentation:\n\n{context}"
            messages.append({"role": "system", "content": context_message})
        
        # Add user message with stronger instruction to prevent thinking process
        messages.append({"role": "user", "content": user_message + "\n\nCRITICAL: Provide ONLY the direct answer with NO explanation of your thought process. Do not mention how you arrived at the answer."})
        
        # Get the API key directly
        api_key = os.environ.get("NVIDIA_API_KEY", "")
        
        # Check if API key is available
        if not api_key:
            print("No Groq API key found, using local response")
            if specific_slide and not slide_exists:
                return format_missing_slide_message(session_id, specific_slide, None, is_error=True)
            fallback_msg = "I'm sorry, but I don't have enough information to answer that question."
            if relevant_slides:
                fallback_msg += f" Your question appears to be about slides: {', '.join([str(num) for num in relevant_slides])}"
            return fallback_msg
        
        # NOTE: we deliberately do not touch socket.setdefaulttimeout here.
        # A process-wide socket default breaks every concurrent NIM call on
        # other threads (batch pipeline, other chat requests). Per-call
        # timeout below is enough.
        try:
            print("Creating NVIDIA NIM client for chat")
            client = OpenAI(api_key=api_key, base_url=NVIDIA_BASE_URL)

            try:
                print("Making NIM API call for chat response")
                completion = client.chat.completions.create(
                    model=VISION_MODEL,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=700,
                    timeout=60.0,
                )
                
                # Extract content from the response with safer access
                if hasattr(completion, 'choices') and completion.choices and completion.choices[0] and hasattr(completion.choices[0], 'message'):
                    message = completion.choices[0].message
                    content = getattr(message, "content", None)
                    # NVIDIA NIM reasoning-style models place the answer in
                    # reasoning_content when content is empty; fall back to it.
                    if not content or content.isspace():
                        content = getattr(message, "reasoning_content", None)

                    if content and not content.isspace():
                        print("Chat response generated successfully")
                        
                        # Check if the content starts with thinking process and remove it
                        # Look for patterns that indicate thinking or meta-commentary
                        thinking_patterns = [
                            r"(?i)Let('s|me|) (me |)think",
                            r"(?i)Let('s|me|) (me |)see",
                            r"(?i)I need to",
                            r"(?i)I'll",
                            r"(?i)First,",
                            r"(?i)Looking at",
                            r"(?i)Based on",
                            r"(?i)According to",
                            r"(?i)The slide",
                            r"(?i)From the",
                            r"(?i)Okay,"
                        ]
                        
                        # Try to find the end of thinking and start of the actual answer
                        for pattern in thinking_patterns:
                            match = re.search(pattern, content)
                            if match:
                                # Check for subsequent paragraph breaks that might indicate transition to answer
                                paragraphs = content.split("\n\n")
                                if len(paragraphs) > 1:
                                    # Remove the first paragraph which is likely thinking
                                    content = "\n\n".join(paragraphs[1:])
                                    break
                        
                        # Handle non-existent slide specifically
                        if specific_slide and not slide_exists:
                            message = format_missing_slide_message(session_id, specific_slide, relevant_slides, is_error=False)
                            return message + "\n\n" + content
                        
                        # Add slide reference if we have relevant slides
                        if specific_slide and slide_exists:
                            return f"{content}\n\n(Information from slide {specific_slide})"
                        elif relevant_slides:
                            return f"{content}\n\n(Information from slides: {', '.join([str(num) for num in relevant_slides])})"
                        return content
                    else:
                        raise ValueError("Empty content in completion response")
                else:
                    raise ValueError("Invalid response structure")
                    
            except Exception as api_error:
                print(f"API error in chat: {str(api_error)}")
                if specific_slide and not slide_exists:
                    slide_data = app.config.get('SLIDE_DATA', {}).get(session_id, {})
                    extraction_data = slide_data.get('extraction_data', {})
                    slide_texts = extraction_data.get('slide_texts', {})
                    available_slides = sorted([int(k) for k in slide_texts.keys()])
                    
                    if available_slides:
                        return format_missing_slide_message(session_id, specific_slide, relevant_slides, is_error=True)
                
                fallback_msg = "I'm sorry, but I encountered a problem processing your request."
                if relevant_slides:
                    fallback_msg += f" Your question appears to be about slides: {', '.join([str(num) for num in relevant_slides])}"
                return fallback_msg
                
        except Exception as e:
            print(f"Error setting up NIM client: {str(e)}")
            fallback_msg = "I'm sorry, but there was a problem connecting to the AI service."
            if relevant_slides:
                fallback_msg += f" Your question appears to be about slides: {', '.join([str(num) for num in relevant_slides])}"
            return fallback_msg

    except Exception as e:
        print(f"Uncaught error in generate_groq_chat_response: {str(e)}")
        return f"I apologize, but I couldn't process your request. Error: {str(e)}"

@app.route('/')
def index():
    """Redirect to the appropriate page based on login status"""
    if current_user.is_authenticated:
        return redirect(url_for('app_index'))
    else:
        return render_template('Landing.html')

@app.route('/health')
def health():
    """Health check endpoint for Google Cloud Run."""
    return jsonify({
        "status": "healthy",
        "timestamp": str(datetime.datetime.now()),
        "service": "Sumora AI",
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
        },
        "libreoffice_available": bool(which("soffice") or which("libreoffice")),
        "nvidia_available": groq_available,
    })


@app.route('/processing_status/<session_id>')
@login_required
def processing_status(session_id):
    """Per-slide pipeline status for the upload progress UI. Returns:
        {
          "overall": "transcribing"|"summarizing"|"retrying"|"complete"|...,
          "total_slides": N,
          "slides": {
              "1": {"transcription": "...", "summary": "...", "retries": n, "error": null},
              ...
          }
        }
    """
    with status_lock:
        sess = slide_status.get(session_id)
        if not sess:
            return jsonify({"error": "unknown session"}), 404
        # Return a defensive copy since the background thread keeps mutating.
        payload = {
            "overall": sess.get("overall"),
            "total_slides": sess.get("total_slides"),
            "slides": {k: dict(v) for k, v in sess.get("slides", {}).items()},
        }
    return jsonify(payload)

@app.route('/app')
@login_required
def app_index():
    """Render the main application page"""
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
@login_required
def upload_slides():
    """Handle slide upload and processing"""
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    # Accept PDF and PowerPoint formats.
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in SUPPORTED_UPLOAD_EXTENSIONS:
        return jsonify({'error': f'Unsupported file type {ext}. Upload PDF or PPTX.'}), 400

    try:
        session_id = str(uuid.uuid4())
        global active_session_id
        active_session_id = session_id

        filename = secure_filename(file.filename)
        upload_dir = os.path.join(tempfile.gettempdir(), 'slide_uploads')
        os.makedirs(upload_dir, exist_ok=True)
        file_path = os.path.join(upload_dir, f"{session_id}_{filename}")
        file.save(file_path)
        print(f"Saved upload: {file_path}")

        # Convert PPTX/PPT to PDF via LibreOffice before the render step.
        try:
            pdf_path = convert_to_pdf_if_needed(file_path, upload_dir)
        except Exception as conv_err:
            print(f"Conversion failure: {conv_err}")
            return jsonify({'error': f'Could not process presentation: {conv_err}'}), 400

        image_paths, total_slides = extract_text_from_pdf(pdf_path, session_id)
        _init_session_status(session_id, total_slides)

        get_or_create_chat_session(session_id)
        app.config['SLIDE_DATA'][session_id] = {
            'extraction_data': {
                'slide_texts': slide_contents_structured.get(session_id, {}),
            },
            'slide_summaries': {},
            'slide_titles': {},
            'image_paths': list(image_paths),
        }

        # Kick off the full async pipeline: transcribe -> summarize -> retry.
        worker = threading.Thread(
            target=process_session_background,
            args=(session_id,),
            daemon=True,
        )
        worker.start()

        return jsonify({
            'success': True,
            'message': 'File uploaded; processing started in background.',
            'session_id': session_id,
            'total_slides': total_slides,
        })

    except Exception as e:
        print(f"Error processing uploaded file: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/chat', methods=['POST'])
@login_required
def chat():
    """Handle chat messages using RAG"""
    data = request.json
    user_message = data.get('message', '')
    session_id = data.get('session_id', active_session_id)
    current_slide = data.get('current_slide')  # Get current slide number from client
    
    # Try to convert current_slide to int if it's provided
    if current_slide:
        try:
            current_slide = int(current_slide)
        except ValueError:
            current_slide = None
    
    # Extract slide number from query if present
    slide_query_match = re.search(r'(?:explain|show|tell me about|what is in|describe|summarize|content of)\s+slide\s+(\d+)(?:\s+|$|\?)', user_message.lower())
    if slide_query_match and not current_slide:
        try:
            extracted_slide = int(slide_query_match.group(1))
            current_slide = extracted_slide
            print(f"Extracted slide number from query: {current_slide}")
        except ValueError:
            pass
    
    if not session_id or session_id not in slide_contents:
        return jsonify({'error': 'No slides have been uploaded or session expired'}), 400
    
    try:
        # Check if the requested slide exists (when specified)
        if current_slide is not None:
            slide_data = app.config.get('SLIDE_DATA', {}).get(session_id, {})
            extraction_data = slide_data.get('extraction_data', {})
            slide_texts = extraction_data.get('slide_texts', {})
            
            if str(current_slide) not in slide_texts:
                # If slide doesn't exist, log it but continue with the query
                # The RAG system will handle this case
                print(f"Requested slide {current_slide} not found. Available slides: {sorted([int(k) for k in slide_texts.keys()])}")
        
        # Use RAG-enhanced chat response
        response_text = generate_groq_chat_response(
            user_message, 
            session_id=session_id, 
            current_slide=current_slide
        )
        
        return jsonify({'response': response_text})
    
    except Exception as e:
        print(f"Error in chat endpoint: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/get_summaries', methods=['GET'])
@login_required
def get_summaries():
    """
    Generate summaries for multiple slides in a single request using optimized token usage
    
    Query parameters:
        session_id (str): The session ID
        slide_nums (str): Comma-separated list of slide numbers to summarize
        force_regenerate (bool, optional): Whether to regenerate summaries even if cached
        
    Returns:
        dict: Mapping of slide numbers to summaries
    """
    try:
        # Get parameters
        session_id = request.args.get('session_id')
        slide_nums_param = request.args.get('slide_nums')
        force_regenerate = request.args.get('force_regenerate', 'false').lower() == 'true'
        
        print(f"get_summaries request: session_id={session_id}, slide_nums={slide_nums_param}, force_regenerate={force_regenerate}")
        
        # Validate parameters
        if not session_id or not slide_nums_param:
            print(f"Missing required parameters: session_id={session_id}, slide_nums={slide_nums_param}")
            return jsonify({"error": "Missing required parameters"}), 400
            
        # Parse slide numbers
        try:
            slide_nums = [int(num.strip()) for num in slide_nums_param.split(',')]
        except ValueError as e:
            print(f"Invalid slide_nums format: {slide_nums_param}. Error: {str(e)}")
            return jsonify({"error": "Invalid slide_nums format. Use comma-separated integers."}), 400
            
        # Get slide data
        slide_data = app.config.get('SLIDE_DATA', {}).get(session_id, {})
        if not slide_data:
            print(f"No data found for session {session_id}")
            return jsonify({"error": f"No data found for session {session_id}"}), 404
            
        # Check if extraction data exists
        extraction_data = slide_data.get('extraction_data', {})
        if not extraction_data:
            print(f"No extraction data found for session {session_id}")
            return jsonify({"error": f"No extraction data found for session {session_id}"}), 404
            
        # Get slide texts
        slide_texts = extraction_data.get('slide_texts', {})
        if not slide_texts:
            print(f"No slide texts found for session {session_id}")
            return jsonify({"error": "No slide texts found"}), 404
            
        # Debug output slide_texts keys
        print(f"Slide text keys in session {session_id}: {list(slide_texts.keys())}")
        
        # Initialize slide_summaries if not present
        if 'slide_summaries' not in slide_data:
            slide_data['slide_summaries'] = {}
            
        # Classify slides into: pending (not yet transcribed), cached, or to-generate.
        slides_to_process = []
        valid_slide_nums = []
        pending_slides = []

        for slide_num in slide_nums:
            str_slide_num = str(slide_num)
            if str_slide_num not in slide_texts:
                print(f"Slide {slide_num} not in slide_texts; available: {list(slide_texts.keys())[:10]}...")
                continue

            slide_text = slide_texts.get(str_slide_num, "")
            if not slide_text.strip():
                # Transcription hasn't landed yet — surface this to the client so
                # the UI can keep polling /processing_status instead of showing
                # "Basic Summary" placeholders forever.
                pending_slides.append(slide_num)
                continue

            if force_regenerate or str_slide_num not in slide_data['slide_summaries']:
                slides_to_process.append(slide_num)
            valid_slide_nums.append(slide_num)
            
        # If we don't need to process any slides, return cached results + pending markers.
        if not slides_to_process:
            result = {}
            for slide_num in valid_slide_nums:
                str_slide_num = str(slide_num)
                if str_slide_num in slide_data['slide_summaries']:
                    result[slide_num] = slide_data['slide_summaries'][str_slide_num]
            if pending_slides:
                result['_pending'] = pending_slides
            print(f"Returning cached summaries for {len(result)} slides, pending={pending_slides}")
            return jsonify(result)
            
        # Process slides that need summarization
        print(f"Processing {len(slides_to_process)} slides for session {session_id}")
        
        # Get or generate presentation overview once for efficiency
        presentation_overview = None
        if 'presentation_overview' in slide_data:
            presentation_overview = slide_data['presentation_overview']
        else:
            try:
                presentation_overview = generate_presentation_overview(session_id)
            except Exception as e:
                print(f"Error generating presentation overview: {str(e)}")
                # Continue without overview
                
        # Process each slide with adjacent-slide context so cross-slide
        # references match what the background pipeline produces.
        results = {}
        total_slides_count = len(slide_texts)

        for slide_num in valid_slide_nums:
            str_slide_num = str(slide_num)

            if str_slide_num in slide_data['slide_summaries'] and not force_regenerate:
                results[slide_num] = slide_data['slide_summaries'][str_slide_num]
                continue
            if str_slide_num not in slide_texts:
                continue

            slide_text = slide_texts.get(str_slide_num, "")
            if not slide_text.strip():
                continue

            neighbor_context = build_neighbor_context_from_texts(slide_texts, slide_num)
            try:
                summary = generate_groq_summary(
                    slide_text=slide_text,
                    slide_num=slide_num,
                    streaming=False,
                    neighbor_context=neighbor_context,
                    total_slides=total_slides_count,
                )
                slide_data['slide_summaries'][str_slide_num] = summary
                results[slide_num] = summary
            except Exception as e:
                print(f"Error generating summary for slide {slide_num}: {str(e)}")
                basic_summary = generate_basic_summary(slide_text, slide_num)
                slide_data['slide_summaries'][str_slide_num] = basic_summary
                results[slide_num] = basic_summary

        if pending_slides:
            results['_pending'] = pending_slides
        return jsonify(results)
        
    except Exception as e:
        print(f"Error in get_summaries: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/static/<path:path>')
def serve_static(path):
    """Serve static files with improved error handling and logging"""
    try:
        print(f"Requested static file: {path}")
        full_path = os.path.join('static', path)
        
        # Check if file exists
        if not os.path.exists(full_path):
            print(f"WARNING: Static file not found: {full_path}")
            # For images, try to provide a fallback
            if path.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                print("Attempting to serve a fallback image")
                return send_from_directory('static', 'images/placeholder.png')
            return "File not found", 404
            
        # Additional logging for Sumora_images
        if 'Sumora_images' in path:
            print(f"Serving Sumora image: {path}, File size: {os.path.getsize(full_path)} bytes")
        
        return send_from_directory('static', path)
    except Exception as e:
        print(f"Error serving static file {path}: {str(e)}")
        return f"Error: {str(e)}", 500

@app.route('/slide_image/<path:path>')
@login_required
def serve_slide_image(path):
    """Serve slide images with proper error handling"""
    try:
        print(f"Requested slide image: {path}")
        
        # Check if path exists directly
        if os.path.exists(path):
            print(f"Serving image from direct path: {path}")
            return send_file(path)
        
        # Try looking in the temporary directory
        temp_path = os.path.join(tempfile.gettempdir(), path)
        if os.path.exists(temp_path):
            print(f"Serving image from temp path: {temp_path}")
            return send_file(temp_path)
            
        # Try with various prefixes from temp directory
        temp_dir = tempfile.gettempdir()
        print(f"Searching for image in temp directory: {temp_dir}")
        temp_matches = []
        try:
            for file in os.listdir(temp_dir):
                if path in file and file.endswith('.png'):
                    full_path = os.path.join(temp_dir, file)
                    temp_matches.append(full_path)
                    
            if temp_matches:
                print(f"Found matching image in temp dir: {temp_matches[0]}")
                return send_file(temp_matches[0])
        except Exception as dir_error:
            print(f"Error searching temp directory: {str(dir_error)}")
            
        # Try with various session prefixes (in case the session ID got separated)
        sessions = list(session_images.keys())
        print(f"Searching across {len(sessions)} sessions for image containing: {path}")
        
        # Try each session prefix
        for session_id in sessions:
            # Check if this image belongs to this session
            for img_path in session_images.get(session_id, []):
                if path in img_path:
                    if os.path.exists(img_path):
                        print(f"Found image in session {session_id}: {img_path}")
                        return send_file(img_path)
                    else:
                        print(f"Found path in session but file doesn't exist: {img_path}")
        
        # If we got here, we couldn't find the image
        print(f"Could not find slide image: {path}")
        print(f"Available sessions: {list(session_images.keys())}")
        for session_id in session_images:
            print(f"Images in session {session_id}: {len(session_images[session_id])}")
        
        # Check if we can still recover by doing a full search in temp
        try:
            print("Performing full scan of temp directory for any slide images")
            all_slides = []
            for file in os.listdir(temp_dir):
                if file.endswith('.png') and ('slide' in file.lower() or 'session' in file.lower()):
                    all_slides.append(os.path.join(temp_dir, file))
                    
            if all_slides:
                print(f"Found {len(all_slides)} slide images in temp dir, using first one as fallback")
                # Use the first slide as a fallback rather than showing nothing
                return send_file(all_slides[0])
        except Exception as e:
            print(f"Error searching for fallback slides: {str(e)}")
        
        # Return a placeholder image
        placeholder_path = os.path.join(app.root_path, 'static', 'images', 'placeholder.png')
        if os.path.exists(placeholder_path):
            print(f"Using placeholder image: {placeholder_path}")
            return send_file(placeholder_path)
        else:
            # Create a simple placeholder image
            print("Creating dynamic placeholder image")
            img = Image.new('RGB', (800, 600), color=(240, 240, 240))
            draw = ImageDraw.Draw(img)
            draw.text((400, 300), "Image not available", fill=(0, 0, 0))
            
            img_io = BytesIO()
            img.save(img_io, 'PNG')
            img_io.seek(0)
            
            return send_file(img_io, mimetype='image/png')
    
    except Exception as e:
        print(f"Error serving slide image {path}: {str(e)}")
        # Create error image
        img = Image.new('RGB', (800, 600), color=(240, 240, 240))
        draw = ImageDraw.Draw(img)
        draw.text((400, 300), f"Error: {str(e)[:100]}", fill=(255, 0, 0))
        
        img_io = BytesIO()
        img.save(img_io, 'PNG')
        img_io.seek(0)
        
        return send_file(img_io, mimetype='image/png')

@app.route('/get_slide_images', methods=['POST'])
@login_required
def get_slide_images():
    """Get slide images separate from summary generation"""
    data = request.json
    session_id = data.get('session_id')
    
    if not session_id:
        print(f"Missing session_id in get_slide_images request")
        return jsonify({'error': 'No session ID provided'}), 400
    
    if session_id not in session_images:
        print(f"Session {session_id} not found in session_images. Available sessions: {list(session_images.keys())}")
        # Try to get slides from SLIDE_DATA as fallback
        if session_id in app.config.get('SLIDE_DATA', {}):
            print(f"Session found in SLIDE_DATA but not in session_images, attempting to rebuild image list")
            # Look for image files that might have this session ID in their name
            all_images = []
            temp_dir = tempfile.gettempdir()
            try:
                for file in os.listdir(temp_dir):
                    if session_id in file and file.endswith('.png'):
                        full_path = os.path.join(temp_dir, file)
                        all_images.append(full_path)
                        
                if all_images:
                    # We found some images, let's use them
                    print(f"Found {len(all_images)} images for session {session_id} in temp directory")
                    session_images[session_id] = all_images
                else:
                    return jsonify({'error': 'No slide images found for this session'}), 404
            except Exception as e:
                print(f"Error trying to rebuild image list: {str(e)}")
                return jsonify({'error': 'No slide images found for this session'}), 404
        else:
            return jsonify({'error': 'No slides have been uploaded or session expired'}), 400
    
    try:
        # Get the slide images for this session
        images = session_images.get(session_id, [])
        print(f"Found {len(images)} images for session {session_id}")
        
        # Create the image paths to be used by the client
        image_paths = []
        for img_path in images:
            # Ensure the file actually exists before sending it to the client
            if not os.path.exists(img_path):
                print(f"Warning: Image file not found: {img_path}")
                continue
                
            # Use just the filename as the path parameter
            filename = os.path.basename(img_path)
            image_paths.append(f"/slide_image/{filename}")
        
        # If we didn't find any valid images, return an error
        if not image_paths:
            print(f"No valid image paths found for session {session_id}")
            return jsonify({'error': 'No valid slide images found'}), 404
            
        print(f"Returning {len(image_paths)} image paths for session {session_id}")
        return jsonify({
            'success': True,
            'slide_image_paths': image_paths,
            'total_slides': len(image_paths)
        })
    except Exception as e:
        print(f"Error getting slide images: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/stream_summary')
@login_required
def stream_summary():
    """
    Stream a summary for a specific slide using SSE
    
    Query parameters:
        session_id (str): The session ID
        slide_num (int): The slide number to summarize
        force_regenerate (bool, optional): Whether to regenerate the summary even if cached
    
    Returns:
        flask.Response: A streaming response containing summary events
    """
    # Get parameters
    session_id = request.args.get('session_id')
    slide_num = request.args.get('slide_num')
    force_regenerate = request.args.get('force_regenerate', 'false').lower() == 'true'
    
    # Validate parameters
    if not session_id or not slide_num:
        print("ERROR: Missing required parameters in stream_summary request")
        return jsonify({"error": "Missing required parameters"}), 400
    
    print(f"Stream summary request received for session {session_id}, slide {slide_num}, force_regenerate={force_regenerate}")
    
    # Generator function to convert JSON response to SSE format
    def event_stream():
        try:
            # First send a progress event to confirm the stream is working
            yield f"event: progress\ndata: Starting summary generation...\n\n"
            
            # Start the actual generation
            count = 0
            for chunk in generate(session_id, slide_num, force_regenerate):
                count += 1
                # Parse JSON chunk
                data = json.loads(chunk)
                
                # Convert to SSE format based on the keys present
                if "error" in data:
                    print(f"Error in generate: {data['error']}")
                    yield f"event: error\ndata: {data['error']}\n\n"
                elif "title" in data:
                    print(f"Title found: {data['title']}")
                    yield f"event: title\ndata: {data['title']}\n\n"
                elif "progress" in data:
                    print(f"Progress: {data['progress']}")
                    yield f"event: progress\ndata: {data['progress']}\n\n"
                elif "summary" in data:
                    print(f"Summary received (length: {len(data['summary'])})")
                    yield f"event: summary\ndata: {data['summary']}\n\n"
                elif "summary_chunk" in data:
                    # Don't log every chunk to avoid console spam
                    if count % 5 == 0:
                        print(f"Chunk {count} received")
                    yield f"event: chunk\ndata: {data['summary_chunk']}\n\n"
                elif "complete" in data:
                    extra = f": {data['error']}" if "error" in data else ""
                    print(f"Summary generation complete{extra}")
                    yield f"event: done\ndata: Summary generation complete{extra}\n\n"
            
            print(f"Processed {count} chunks for session {session_id}, slide {slide_num}")
            
        except Exception as e:
            print(f"ERROR in stream_summary: {str(e)}")
            # Send detailed error back to client
            yield f"event: error\ndata: Unexpected error: {str(e)}\n\n"
    
    # Return streaming response
    print(f"Starting event stream for session {session_id}, slide {slide_num}")
    response = Response(event_stream(), mimetype="text/event-stream")
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'  # For Nginx
    response.headers['Access-Control-Allow-Origin'] = '*'  # Allow cross-origin requests
    return response

def generate(session_id, slide_num, force_regenerate=False):
    """
    Generate a summary for a specific slide with simplified error handling.
    This function yields each piece of the summary as it's generated.
    
    Args:
        session_id (str): The session ID
        slide_num (int or str): The slide number to summarize
        force_regenerate (bool, optional): Whether to regenerate the summary even if cached
        
    Yields:
        str: Each piece of the summary as it's generated
    """
    print(f"\n=== Starting generation process for slide {slide_num} ===")
    
    # Validate inputs
    if not session_id:
        print("Missing session_id parameter")
        yield json.dumps({"error": "Missing session_id parameter"})
        return
        
    try:
        slide_num = int(slide_num)
    except ValueError:
        print(f"Invalid slide_num parameter: {slide_num}")
        yield json.dumps({"error": "Invalid slide_num parameter"})
        return
    
    # Safely access slide data
    slide_data = app.config.get('SLIDE_DATA', {}).get(session_id, {})
    if not slide_data:
        print(f"No data found for session {session_id}")
        yield json.dumps({"error": f"No data found for session {session_id}"})
        return
    
    # Check if extraction data exists
    extraction_data = slide_data.get('extraction_data', {})
    if not extraction_data:
        print(f"No extraction data found for session {session_id}")
        yield json.dumps({"error": f"No extraction data found for session {session_id}"})
        return
    
    # Get slide texts
    slide_texts = extraction_data.get('slide_texts', {})
    if not slide_texts:
        print("No slide texts found")
        yield json.dumps({"error": "No slide texts found"})
        return
    
    # Validate slide number
    str_slide_num = str(slide_num)
    if str_slide_num not in slide_texts:
        print(f"Slide {slide_num} not found")
        yield json.dumps({"error": f"Slide {slide_num} not found"})
        return
    
    # Get the slide text. If the background vision pipeline hasn't transcribed
    # this slide yet, wait for it — yielding progress events so the client can
    # display "Transcribing slide N..." instead of a generic error.
    slide_text = slide_texts.get(str_slide_num, "")
    if not slide_text.strip():
        print(f"Slide {slide_num} not transcribed yet; waiting for pipeline")
        waited = False
        for event in wait_for_slide_ready(session_id, slide_num):
            if "progress" in event:
                waited = True
                yield json.dumps(event)
                continue
            if event.get("ready"):
                slide_text = slide_texts.get(str_slide_num, "")
                break
            # Not ready and we're done waiting.
            reason = event.get("reason", "unknown")
            if reason == "failed":
                detail = event.get("error") or "transcription failed"
                yield json.dumps({
                    "error": f"Slide {slide_num} could not be transcribed ({detail}). "
                             "Try regenerating — it may succeed on another attempt."
                })
            elif reason == "timeout":
                yield json.dumps({
                    "error": f"Slide {slide_num} is still being processed. Give it a few more seconds and try again."
                })
            else:
                yield json.dumps({"error": f"Slide {slide_num} is not available yet."})
            return

        if not slide_text.strip():
            yield json.dumps({"error": f"Slide {slide_num} is empty after transcription."})
            return
        if waited:
            # Refresh the client — transcription finished, summary generation starting.
            yield json.dumps({"progress": f"Generating summary for slide {slide_num}..."})

    # Initialize slide_summaries if not present
    if 'slide_summaries' not in slide_data:
        slide_data['slide_summaries'] = {}

    # Check if we already have the summary cached and force_regenerate is False
    if str_slide_num in slide_data['slide_summaries'] and not force_regenerate:
        cached_summary = slide_data['slide_summaries'][str_slide_num]
        print(f"Using cached summary for slide {slide_num}")
        title = extract_slide_title(slide_text, slide_num)
        yield json.dumps({"title": title})
        yield json.dumps({"summary": cached_summary})
        yield json.dumps({"complete": True})
        return

    yield json.dumps({"progress": "Generating summary..."})
    title = extract_slide_title(slide_text, slide_num)
    yield json.dumps({"title": title})

    # Build adjacent-slide context so interactive summaries carry the same
    # cross-slide referencing the batch pipeline produces.
    neighbor_context = build_neighbor_context_from_texts(slide_texts, slide_num)
    total_slides_count = len(slide_texts)

    try:
        print(f"Generating summary for slide {slide_num}")
        completion_stream = generate_groq_summary(
            slide_text=slide_text,
            slide_num=slide_num,
            streaming=True,
            neighbor_context=neighbor_context,
            total_slides=total_slides_count,
        )
        
        # Process the streaming response
        summary_chunks = []
        
        # Track if we get any chunks
        got_chunks = False
        
        # Process the streaming response
        for chunk in completion_stream:
            got_chunks = True
            # Add chunk to collection for later storage
            summary_chunks.append(chunk)
            # Yield this chunk
            yield json.dumps({"summary_chunk": chunk})
            
        # Check if we received any chunks
        if not got_chunks:
            # If no chunks were received, use non-streaming as fallback
            print(f"No chunks received, using non-streaming fallback for slide {slide_num}")
            complete_summary = generate_groq_summary(
                slide_text=slide_text,
                slide_num=slide_num,
                streaming=False,
                neighbor_context=neighbor_context,
                total_slides=total_slides_count,
            )
            
            # Store and yield the non-streaming result
            slide_data['slide_summaries'][str_slide_num] = complete_summary
            yield json.dumps({"summary": complete_summary})
        else:
            # We got chunks, combine them
            complete_summary = "".join(summary_chunks)
            
            # Store the summary
            slide_data['slide_summaries'][str_slide_num] = complete_summary
        
        # Return completion confirmation
        yield json.dumps({"complete": True})
            
    except Exception as e:
        print(f"Error in generate function: {str(e)}")
        
        # Use generate_basic_summary as a final fallback
        print(f"Using basic summary generation as final fallback for slide {slide_num}")
        basic_summary = generate_basic_summary(slide_text, slide_num)
        slide_data['slide_summaries'][str_slide_num] = basic_summary
        
        # Yield the basic summary
        yield json.dumps({"summary": basic_summary})
        yield json.dumps({"complete": True, "error": str(e)})

# Function to generate a presentation overview using RAG
def generate_presentation_overview(session_id):
    """
    Generate a concise overview of the entire presentation that can be used
    as shared context for individual slide summaries.
    
    Args:
        session_id (str): The session ID
        
    Returns:
        str: The presentation overview
    """
    # Check if we already have an overview cached
    slide_data = app.config.get('SLIDE_DATA', {}).get(session_id, {})
    
    # Use a faster memory cache lookup first
    global_cache_key = f"overview_{session_id}"
    if global_cache_key in summary_cache:
        print(f"Using cached presentation overview for {session_id}")
        return summary_cache[global_cache_key]
    
    # Check session data cache
    if 'presentation_overview' in slide_data:
        print(f"Using session cached presentation overview for {session_id}")
        # Add to global cache for faster lookup next time
        summary_cache[global_cache_key] = slide_data['presentation_overview']
        return slide_data['presentation_overview']
    
    # Get slide texts
    extraction_data = slide_data.get('extraction_data', {})
    slide_texts = extraction_data.get('slide_texts', {})
    
    if not slide_texts:
        print("No slide texts found")
        return ""
    
    # Identify key slides (first, last, and some in the middle)
    slide_nums = sorted([int(k) for k in slide_texts.keys()])
    if not slide_nums:
        return ""
    
    # Maximum number of slides to use in overview generation (to limit token usage)
    MAX_KEY_SLIDES = 5
    
    # Always include first and last slides
    key_slides = [slide_nums[0], slide_nums[-1]]
    
    # Add middle slide for any presentation
    if len(slide_nums) > 2:
        middle = slide_nums[len(slide_nums) // 2]
        if middle not in key_slides:
            key_slides.append(middle)
    
    # Add additional slides if we have a long presentation (equally spaced)
    if len(slide_nums) > 10 and len(key_slides) < MAX_KEY_SLIDES:
        remaining_slots = MAX_KEY_SLIDES - len(key_slides)
        segment_size = len(slide_nums) // (remaining_slots + 1)
        for i in range(1, remaining_slots + 1):
            position = segment_size * i
            if 0 <= position < len(slide_nums):
                candidate = slide_nums[position]
                if candidate not in key_slides:
                    key_slides.append(candidate)
    
    # Sort key slides
    key_slides = sorted(key_slides)
    
    print(f"Generating overview for {session_id} with {len(key_slides)} key slides: {key_slides}")
    
    # Extract key slide content with limited token usage
    MAX_CHARS_PER_SLIDE = 250  # Limit characters per slide to save tokens
    
    # First get titles for all slides (lightweight)
    all_slide_titles = {}
    for slide_num in slide_nums:
        slide_text = slide_texts.get(str(slide_num), "")
        title = extract_slide_title(slide_text, slide_num)
        all_slide_titles[slide_num] = title
    
    # Create quick overview from just titles as fallback
    toc_overview = f"**Presentation Overview**\n\n"
    # Add every 3rd title plus first and last
    for slide_num in slide_nums:
        if slide_num % 3 == 0 or slide_num == slide_nums[0] or slide_num == slide_nums[-1]:
            toc_overview += f"- Slide {slide_num}: {all_slide_titles[slide_num]}\n"
    
    # Create content from key slides (limited)
    key_content_parts = []
    for slide_num in key_slides:
        slide_text = slide_texts.get(str(slide_num), "")
        if slide_text:
            # Limit text length
            if len(slide_text) > MAX_CHARS_PER_SLIDE:
                words = slide_text.split()
                slide_text = " ".join(words[:MAX_CHARS_PER_SLIDE//10]) + "..."  # ~10 chars per word average
            
            title = all_slide_titles[slide_num]
            key_content_parts.append(f"Slide {slide_num} - {title}: {slide_text}")
    
    # Join key content
    key_content = "\n\n".join(key_content_parts)
    
    # Get the API key directly
    api_key = os.environ.get("NVIDIA_API_KEY", "")
    
    # Check if API key is available
    if not api_key:
        print("No Groq API key found, using local overview")
        return toc_overview
    
    # NOTE: no socket.setdefaulttimeout; see generate_groq_summary for rationale.
    try:
        # Create ultra-compact prompt for overview generation
        prompt = f"""
Create a brief overview of this presentation based on the provided key slides.

KEY SLIDES:
{key_content}

Format:
1. One sentence description of the main topic
2. 3-5 bullet points for key themes
3. Be extremely concise
"""

        # Create minimal messages for the API call
        system_message = "You create extremely concise presentation overviews. Be brief and informative."
        messages = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt}
        ]
        
        try:
            # Create the client
            print("Creating Groq client for overview")
            client = OpenAI(api_key=api_key, base_url=NVIDIA_BASE_URL)
            
            try:
                print("Making NIM API call for presentation overview")
                response = client.chat.completions.create(
                    model=VISION_MODEL,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=300,
                    timeout=60.0,
                )
                
                # Get overview content
                if hasattr(response, 'choices') and response.choices and hasattr(response.choices[0], 'message'):
                    message = response.choices[0].message
                    overview = getattr(message, "content", None) or getattr(message, "reasoning_content", "")
                    
                    # Ensure we start with a heading or bullet
                    if not overview.startswith('#') and not overview.startswith('*') and not overview.startswith('-'):
                        overview = f"# Presentation Overview\n\n{overview}"
                    
                    # Add abbreviated table of contents
                    toc = "\n\n**Key Slides:**\n"
                    for slide_num in key_slides:
                        title = all_slide_titles[slide_num]
                        toc += f"- Slide {slide_num}: {title}\n"
                    
                    overview += toc
                    
                    # Cache the overview
                    slide_data['presentation_overview'] = overview
                    summary_cache[global_cache_key] = overview
                    
                    print(f"Successfully generated overview ({len(overview)} chars)")
                    return overview
                else:
                    raise ValueError("Invalid response structure")
                    
            except Exception as api_error:
                print(f"API error in overview generation: {str(api_error)}")
                return toc_overview
                
        except Exception as e:
            print(f"Error setting up NIM client for overview: {str(e)}")
            return toc_overview

    except Exception as e:
        print(f"Uncaught error in generate_presentation_overview: {str(e)}")
        return toc_overview

# Add this function after the existing functions
def _chunked(iterable, size):
    """Yield lists of at most `size` items from iterable, preserving order."""
    buf = []
    for item in iterable:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def _run_transcription_batch(session_id, image_paths_by_slide, slide_nums):
    """Transcribe the given slide numbers concurrently (they share a batch
    window). Returns the list of slide nums that FAILED. Successes are written
    into slide_contents_structured[session_id] and slide_status.
    """
    failures = []
    structured = slide_contents_structured.setdefault(session_id, {})

    def _work(n):
        path = image_paths_by_slide.get(n)
        if not path or not os.path.isfile(path):
            raise RuntimeError(f"no image for slide {n}")
        return vision_transcribe_slide(path, n)

    with ThreadPoolExecutor(max_workers=min(len(slide_nums), PROCESSING_BATCH_SIZE)) as pool:
        future_to_slide = {pool.submit(_work, n): n for n in slide_nums}
        for fut in as_completed(future_to_slide):
            n = future_to_slide[fut]
            try:
                text = fut.result()
                structured[str(n)] = text
                _update_slide_state(session_id, n, transcription="done", error=None)
                print(f"[{session_id[:8]}] transcribed slide {n} ({len(text)} chars)")
            except Exception as exc:
                failures.append(n)
                _update_slide_state(session_id, n, transcription="failed",
                                    error=f"transcribe: {exc}")
                print(f"[{session_id[:8]}] transcription FAILED slide {n}: {exc}")
    return failures


def _run_summary_batch(session_id, slide_nums):
    """Generate concept-dense summaries for the given slides using the
    transcribed text and neighbor context. Returns list of failed slide nums."""
    failures = []
    structured = slide_contents_structured.get(session_id, {})
    slide_data = app.config.get('SLIDE_DATA', {}).get(session_id, {})
    summaries = slide_data.setdefault('slide_summaries', {})
    total = len(structured)

    def _work(n):
        text = structured.get(str(n), "")
        if not text.strip():
            raise RuntimeError("empty transcription (transcription must succeed first)")
        neighbors = _collect_neighbor_context(structured, n, total)
        return generate_groq_summary(
            slide_text=text, slide_num=n, streaming=False,
            neighbor_context=neighbors, total_slides=total,
        )

    with ThreadPoolExecutor(max_workers=min(len(slide_nums), PROCESSING_BATCH_SIZE)) as pool:
        future_to_slide = {pool.submit(_work, n): n for n in slide_nums}
        for fut in as_completed(future_to_slide):
            n = future_to_slide[fut]
            try:
                summary = fut.result()
                if not summary or (isinstance(summary, str) and not summary.strip()):
                    raise RuntimeError("empty summary")
                summaries[str(n)] = summary
                _update_slide_state(session_id, n, summary="done", error=None)
                print(f"[{session_id[:8]}] summarized slide {n}")
            except Exception as exc:
                failures.append(n)
                _update_slide_state(session_id, n, summary="failed",
                                    error=f"summarize: {exc}")
                print(f"[{session_id[:8]}] summary FAILED slide {n}: {exc}")
    return failures


def _collect_neighbor_context(structured, slide_num, total, radius=2, max_chars=500):
    """Return a compact string of up to `radius` previous+next slide
    transcriptions, truncated to keep the prompt short."""
    parts = []
    for offset in range(-radius, radius + 1):
        if offset == 0:
            continue
        n = slide_num + offset
        if n < 1 or n > total:
            continue
        text = (structured.get(str(n)) or "").strip()
        if not text:
            continue
        snippet = text[:max_chars].strip()
        parts.append(f"Slide {n}:\n{snippet}")
    return "\n\n".join(parts)


def process_session_background(session_id):
    """End-to-end async pipeline for a session:
        1. Transcribe all slides, 5 at a time, with a rate-limit-safe pause.
        2. Re-index RAG after every batch so chat/search catch up progressively.
        3. Summarize all slides, 5 at a time, using transcription + neighbor context.
        4. Retry failed transcriptions (groups of up to 5), then failed summaries.
        5. Final RAG re-index to include any retry-recovered slides.
    """
    print(f"[{session_id[:8]}] pipeline start")
    slide_data = app.config.get('SLIDE_DATA', {}).get(session_id)
    if not slide_data:
        print(f"[{session_id[:8]}] no SLIDE_DATA, aborting")
        return

    image_paths = slide_data.get('image_paths') or session_images.get(session_id) or []
    image_paths_by_slide = {i + 1: p for i, p in enumerate(image_paths)}
    total = len(image_paths)
    if total == 0:
        _set_overall_state(session_id, "failed")
        return

    # --- Phase 1: transcription ---
    _set_overall_state(session_id, "transcribing")
    failed_transcribe = []
    for batch in _chunked(range(1, total + 1), PROCESSING_BATCH_SIZE):
        for n in batch:
            _update_slide_state(session_id, n, transcription="processing")
        fails = _run_transcription_batch(session_id, image_paths_by_slide, batch)
        failed_transcribe.extend(fails)
        try:
            create_slide_embeddings(session_id, slide_contents_structured.get(session_id, {}))
        except Exception as re_err:
            print(f"[{session_id[:8]}] incremental RAG reindex failed: {re_err}")
        time.sleep(PROCESSING_BATCH_DELAY_SECONDS)

    # --- Phase 1.5: retry failed transcriptions ---
    retry_round = 0
    pending = list(failed_transcribe)
    while pending and retry_round < MAX_RETRIES_PER_SLIDE:
        retry_round += 1
        _set_overall_state(session_id, "retrying")
        print(f"[{session_id[:8]}] transcription retry round {retry_round}: {pending}")
        # Breathing room between retry rounds so transient NIM 5xx / rate
        # limits have a chance to clear before we hammer the same slides.
        time.sleep(RETRY_BACKOFF_SECONDS)
        next_pending = []
        for group in _chunked(pending, RETRY_BATCH_SIZE):
            for n in group:
                _update_slide_state(session_id, n, transcription="processing",
                                    retries=retry_round)
            fails = _run_transcription_batch(session_id, image_paths_by_slide, group)
            next_pending.extend(fails)
            try:
                create_slide_embeddings(session_id, slide_contents_structured.get(session_id, {}))
            except Exception as re_err:
                print(f"[{session_id[:8]}] retry RAG reindex failed: {re_err}")
            time.sleep(PROCESSING_BATCH_DELAY_SECONDS)
        pending = next_pending

    # --- Phase 2: summarization ---
    _set_overall_state(session_id, "summarizing")
    # Only summarize slides that have transcriptions.
    structured = slide_contents_structured.get(session_id, {})
    summarizable = [n for n in range(1, total + 1) if (structured.get(str(n)) or "").strip()]
    failed_summary = []
    for batch in _chunked(summarizable, PROCESSING_BATCH_SIZE):
        for n in batch:
            _update_slide_state(session_id, n, summary="processing")
        fails = _run_summary_batch(session_id, batch)
        failed_summary.extend(fails)
        time.sleep(PROCESSING_BATCH_DELAY_SECONDS)

    # --- Phase 2.5: retry failed summaries ---
    retry_round = 0
    pending = list(failed_summary)
    while pending and retry_round < MAX_RETRIES_PER_SLIDE:
        retry_round += 1
        _set_overall_state(session_id, "retrying")
        print(f"[{session_id[:8]}] summary retry round {retry_round}: {pending}")
        time.sleep(RETRY_BACKOFF_SECONDS)
        next_pending = []
        for group in _chunked(pending, RETRY_BATCH_SIZE):
            for n in group:
                _update_slide_state(session_id, n, summary="processing",
                                    retries=retry_round)
            fails = _run_summary_batch(session_id, group)
            next_pending.extend(fails)
            time.sleep(PROCESSING_BATCH_DELAY_SECONDS)
        pending = next_pending

    # --- Final RAG sync + presentation overview ---
    try:
        create_slide_embeddings(session_id, slide_contents_structured.get(session_id, {}))
    except Exception as re_err:
        print(f"[{session_id[:8]}] final RAG reindex failed: {re_err}")

    try:
        generate_presentation_overview(session_id)
    except Exception as ov_err:
        print(f"[{session_id[:8]}] overview generation failed: {ov_err}")

    # Determine final overall state.
    with status_lock:
        sess = slide_status.get(session_id, {})
        slides = sess.get("slides", {})
        all_done = all(s.get("summary") == "done" for s in slides.values())
        any_done = any(s.get("summary") == "done" for s in slides.values())
        sess["overall"] = "complete" if all_done else ("partial" if any_done else "failed")

    print(f"[{session_id[:8]}] pipeline done")


# Legacy alias so any stray references keep working.
generate_all_summaries_background = process_session_background

def get_available_slides(session_id):
    """
    Get all available slide numbers for a session
    
    Args:
        session_id (str): The session ID
        
    Returns:
        list: List of available slide numbers sorted in ascending order
    """
    try:
        # Get slide data
        slide_data = app.config.get('SLIDE_DATA', {}).get(session_id, {})
        if not slide_data:
            return []
            
        # Get slide texts
        extraction_data = slide_data.get('extraction_data', {})
        if not extraction_data:
            return []
            
        slide_texts = extraction_data.get('slide_texts', {})
        if not slide_texts:
            return []
            
        # Convert keys to integers and sort
        return sorted([int(k) for k in slide_texts.keys()])
        
    except Exception as e:
        print(f"Error getting available slides: {str(e)}")
        return []

def format_missing_slide_message(session_id, slide_num, relevant_slides=None, is_error=False):
    """
    Format a consistent message for when a slide doesn't exist.
    
    Args:
        session_id (str): The session ID
        slide_num (int): The requested slide number
        relevant_slides (list, optional): List of relevant slide numbers found by RAG
        is_error (bool, optional): Whether this is an error message (True) or informational (False)
        
    Returns:
        str: Formatted message about the missing slide
    """
    available_slides = get_available_slides(session_id)
    
    if not available_slides:
        return f"Slide {slide_num} was not found. It appears there are no slides in this presentation."
    
    if is_error:
        prefix = f"I cannot find Slide {slide_num} in this presentation."
    else:
        prefix = f"Note: Slide {slide_num} does not exist in this presentation."
    
    # Basic information about available slides
    message = f"{prefix} The presentation contains {len(available_slides)} slides (numbered {min(available_slides)}-{max(available_slides)})."
    
    # Find closest slides to the requested one
    closest_slides = []
    for available_num in available_slides:
        if abs(available_num - slide_num) <= 2:  # Within 2 slides
            closest_slides.append(available_num)
    
    if closest_slides:
        message += f" Nearby slides are: {', '.join([str(num) for num in sorted(closest_slides)])}."
    
    # Add information about relevant slides if available
    if relevant_slides:
        if is_error:
            message += f" Your question may relate to content found in slides: {', '.join([str(num) for num in relevant_slides])}."
        else:
            message += f" Information was retrieved from slides: {', '.join([str(num) for num in relevant_slides])}."
    
    return message

# Add login page route
@app.route('/login')
def login():
    """Show login page"""
    error = request.args.get('error')
    return render_template('login.html', error=error)

# Add Google login route
@app.route('/google_login')
def google_login():
    """Redirect to Google for authentication"""
    # Generate and store CSRF state token
    redirect_uri = f"{base_url}/auth/google/callback"
    return google.authorize_redirect(redirect_uri)

# Add Google callback route
@app.route('/auth/google/callback')
def google_callback():
    """Handle Google OAuth callback"""
    try:
        # Exchange authorization code for access token
        token = google.authorize_access_token()
        
        # Get user info from Google
        userinfo = google.parse_id_token(token, nonce=session.get('nonce'))
        
        # If parse_id_token fails or doesn't return expected data, fallback to userinfo endpoint
        if not userinfo or 'email' not in userinfo:
            resp = google.get('userinfo')
            userinfo = resp.json()
        
        # Validate user info
        if not userinfo or 'email' not in userinfo:
            raise ValueError("Failed to obtain user information from Google")
        
        # Create user object
        user = User(
            id=userinfo['sub'] if 'sub' in userinfo else userinfo['id'],
            email=userinfo['email'],
            name=userinfo.get('name', userinfo['email'])
        )
        
        # Store user data in session
        session['user_data'] = {
            'id': user.id,
            'email': user.email,
            'name': user.name
        }
        
        # Login user with Flask-Login
        login_user(user)
        
        # Redirect to app page
        return redirect(url_for('app_index'))
    except Exception as e:
        # Log the error
        app.logger.error(f"OAuth error: {str(e)}")
        
        # Clear any existing session data that might be causing issues
        session.clear()
        
        # Redirect back to login page
        return redirect(url_for('login', error="Authentication failed. Please try again."))

# Add logout route
@app.route('/logout')
def logout():
    """Log the user out and redirect to login page"""
    logout_user()
    session.clear()
    return redirect(url_for('login'))

# Add terms of service route
@app.route('/tos')
def terms_of_service():
    """Display the Terms of Service page"""
    return render_template('tos.html')

# Add privacy policy route
@app.route('/privacy-policy')
def privacy_policy():
    """Display the Privacy Policy page"""
    return render_template('privacy-policy.html')

# Add route for misspelled privacy policy URL (for Google OAuth)
@app.route('/privicy-policy')
def privicy_policy():
    """Redirect misspelled privacy policy URL to correct one"""
    return redirect(url_for('privacy_policy'))
    
# Run the app
if __name__ == '__main__':
    # For Cloud Run, use a simpler startup approach focused on reliability
    try:
        port = int(os.environ.get('PORT', 5002))
        print(f"Starting application on port {port}")
        print(f"Running in {'Production' if 'K_SERVICE' in os.environ else 'Development'} mode")
        
        # Simple but reliable startup - just run with the right host and port
        app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
    except Exception as e:
        print(f"ERROR STARTING APPLICATION: {str(e)}")
        import traceback
        traceback.print_exc()
        raise
