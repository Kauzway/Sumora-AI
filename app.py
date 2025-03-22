import os
# Removed Gemini import
from flask import Flask, request, jsonify, render_template, send_from_directory, send_file, Response
import fitz  # PyMuPDF for PDF processing
from werkzeug.utils import secure_filename
import uuid
import time
from functools import lru_cache
import tempfile
import base64
from io import BytesIO
from PIL import Image, ImageDraw
import pytesseract  # For OCR on slide images
import requests
import json
import re
from openai import OpenAI
from groq import Groq  # Import Groq client
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

# Configure Tesseract path for OCR
# For Windows, you need to set the Tesseract executable path
if platform.system() == 'Windows':
    # Default path for Tesseract on Windows
    tesseract_path = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    
    # Try to find tesseract.exe in PATH
    tesseract_in_path = which('tesseract')
    if tesseract_in_path:
        tesseract_path = tesseract_in_path
        
    pytesseract.pytesseract.tesseract_cmd = tesseract_path
    print(f"Set Tesseract path to: {tesseract_path}")
else:
    # On Linux (Cloud Run) and MacOS, we assume Tesseract is in the PATH
    # No need to set the path explicitly as pytesseract will use the system's default
    print("Using system Tesseract installation from PATH")

# Check if tesseract is available
tesseract_available = False  # Default to False, will set to True if test passes
try:
    # Test if tesseract is working by doing a simple OCR on a small image
    from PIL import Image
    test_img = Image.new('RGB', (50, 10), color = (255, 255, 255))
    pytesseract.image_to_string(test_img)
    tesseract_available = True
    print("✅ Tesseract OCR is working properly")
except Exception as e:
    print(f"⚠️ Tesseract OCR is not available: {str(e)}")
    print("Text extraction from images will be limited. Please install Tesseract OCR for full functionality.")

# Define TimeoutError if it doesn't exist (for Python <3.3 compatibility)
try:
    TimeoutError
except NameError:
    class TimeoutError(Exception):
        pass

# Load environment variables right after imports, before any code execution
load_dotenv()  # Take environment variables from .env file

# Rate limiting configuration for Groq API
RATE_LIMIT_RPM = 1800  # Requests per minute (for llama-3.1-8b-instant)
RATE_LIMIT_RPD = 900000  # Requests per day (for llama-3.1-8b-instant)
RATE_LIMIT_TPM = 450000  # Tokens per minute (for llama-3.1-8b-instant)

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

# Configure Groq API for models
groq_api_key = os.environ.get("GROQ_API_KEY")
if not groq_api_key:
    print("⚠️ WARNING: No Groq API key found in environment variables")
    print("Set your GROQ_API_KEY environment variable for AI functionality to work")
    groq_api_key = ""  # Empty string instead of hardcoded key

# Initialize Groq client - will be initialized properly when API key is available
client = None
if groq_api_key:
    client = Groq(api_key=groq_api_key)
    print("✅ Groq client initialized successfully")

# Model configuration - use Llama 3.1 8B model
groq_model = "llama-3.1-8b-instant"  # The Groq model name

# Variable to track if Groq API is available
groq_available = True

# Global dictionaries for storage
slide_contents = {}  # Store slide content by session ID
slide_contents_structured = {}  # Store structured slide content by session ID
slide_images = {}  # Store image paths by session ID
session_images = {}  # Store image paths for each session
chat_sessions = {}  # Store chat history by session ID
summary_cache = {}  # Cache for summaries to avoid redundant API calls

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

# Global variables
active_session_id = None

def extract_text_from_pdf(pdf_path, session_id):
    """Extract text from PDF and create a structured dataset for the session"""
    try:
        document = fitz.open(pdf_path)
        print(f"Opened PDF: {pdf_path} with {len(document)} pages")
        
        text_content = ""
        slide_images = []
        structured_slides = {}  # Dictionary to store structured slide text by slide number
        
        # For tracking the session's slide images
        if session_id not in session_images:
            session_images[session_id] = []
        
        for i, page in enumerate(document):
            slide_num = i + 1  # 1-indexed slide numbers
            str_slide_num = str(slide_num)  # Convert to string for consistent dictionary keys
            
            # Extract text from this page
            page_text = page.get_text()
            
            # Render page to image for display and OCR
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2x zoom for better quality
            img_data = pix.tobytes("png")
            
            # Save image to temp file
            img_path = tempfile.mktemp(suffix='.png', prefix=f'{session_id}_slide{slide_num}_')
            with open(img_path, 'wb') as img_file:
                img_file.write(img_data)
            
            # Record image path
            slide_images.append(img_path)
            session_images[session_id].append(img_path)
            
            # Check if the page has minimal text content and might be mostly image-based
            has_minimal_text = len(page_text.strip().split()) < 15
            
            if has_minimal_text and tesseract_available:
                print(f"Slide {slide_num} has minimal text, attempting OCR...")
                try:
                    # Open the image with PIL for OCR
                    with Image.open(img_path) as img:
                        # Extract text using OCR
                        ocr_text = pytesseract.image_to_string(img)
                        
                        if ocr_text and len(ocr_text.strip()) > 0:
                            # Combine OCR text with any existing text
                            combined_text = page_text.strip() + "\n\n[OCR-extracted text:]\n" + ocr_text
                            
                            # Add to the combined text
                            text_content += f"Slide {slide_num}:\n{combined_text}\n\n"
                            
                            # Store in structured dictionary
                            structured_slides[str_slide_num] = combined_text
                            
                            print(f"Successfully extracted OCR text from slide {slide_num}")
                            continue  # Skip the regular text addition below
                        else:
                            print(f"OCR didn't extract any text from slide {slide_num}")
                except Exception as ocr_err:
                    print(f"Error during OCR processing for slide {slide_num}: {str(ocr_err)}")
            elif has_minimal_text and not tesseract_available:
                # If OCR isn't available but the slide has minimal text, add a note
                print(f"Slide {slide_num} has minimal text, but OCR is not available (running on Cloud Run)")
                # Add metadata to indicate this might be an image-heavy slide
                page_text += "\n\n[This slide appears to be primarily visual with limited text. OCR is not available to extract text from images.]"
            
            # For slides where OCR wasn't performed or failed, use the regular text
            # Add to the combined text
            text_content += f"Slide {slide_num}:\n{page_text}\n\n"
            
            # Store in structured dictionary for RAG processing - use string keys consistently
            structured_slides[str_slide_num] = page_text  # Use string keys for slide numbers
            
        document.close()
        
        # Store the raw text content
        slide_contents[session_id] = text_content
        
        # Store structured slide content (separate dictionary for each slide)
        slide_contents_structured[session_id] = structured_slides
        
        # Create embeddings for RAG retrieval
        create_slide_embeddings(session_id, structured_slides)
        
        return text_content, slide_images
        
    except Exception as e:
        print(f"Error extracting text from PDF: {str(e)}")
        raise e

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

def generate_groq_summary(slide_text, slide_num, streaming=True):
    """
    Generate a summary using Groq API with more robust error handling
    
    Args:
        slide_text (str): The text of the slide to summarize
        slide_num (int): The slide number
        streaming (bool): Whether to use streaming API
        
    Returns:
        str or generator: Either a string with the summary or a generator yielding chunks
    """
    try:
        print(f"\n=== Starting Groq summary generation for slide {slide_num} ===")
        
        # Get the API key directly
        api_key = os.environ.get("GROQ_API_KEY", "")
        
        # Check if API key is available
        if not api_key:
            print("No Groq API key found, using local generation")
            return generate_basic_summary(slide_text, slide_num)
        
        # Set a quick timeout for faster fallback if API is unresponsive
        import socket
        original_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(5.0)  # 5 second timeout
        
        try:
            # Create the client
            print("Creating Groq client")
            client = Groq(api_key=api_key)
            
            # Check if this slide contains OCR-extracted text
            has_ocr = "[OCR-extracted text:]" in slide_text
            
            # Prepare the input message
            system_prompt = """You are an expert presentation analyzer focusing on creating clear, concise summaries. Summarize the given slide content with these guidelines:
1. Begin with the main point or purpose of the slide
2. Include key facts, figures, and important takeaways
3. Use bullet points if appropriate for clarity
4. Keep the summary concise (2-4 sentences)
5. Do not add information not present in the slide content
6. Format may include markdown for highlighting key elements
"""

            # Add OCR-specific instructions if OCR text is present
            if has_ocr:
                system_prompt += """
7. This slide contains OCR-extracted text from images. Focus on combining both regular text and OCR text to create a comprehensive summary.
8. If the OCR text seems to contain errors, use your judgment to interpret what the correct text might be, but stay close to the content.
"""
            
            # Estimate tokens for the request
            est_prompt_tokens = len(slide_text) // 4 + 150  # System prompt + slide content
            est_completion_tokens = 250  # Summary length
            est_total_tokens = est_prompt_tokens + est_completion_tokens
            
            user_message = f"Slide {slide_num} content:\n\n{slide_text}\n\nPlease provide a clear, concise summary of this slide."
            
            # Create messages
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ]
            
            # Handle streaming mode
            if streaming:
                print(f"Making Groq API streaming call for slide {slide_num}")
                try:
                    # Create streaming call with timeout
                    stream = client.chat.completions.create(
                        model="llama-3.1-8b-instant",
                        messages=messages,
                        temperature=0.3,
                        max_tokens=500,
                        stream=True,
                        timeout=8.0  # Set an explicit timeout for the API call
                    )
                    
                    # Process streaming response
                    def process_stream():
                        try:
                            # Keep track of timeout
                            start_time = time.time()
                            timeout_seconds = 8.0
                            
                            for chunk in stream:
                                # Check for timeout during streaming
                                if time.time() - start_time > timeout_seconds:
                                    print(f"Streaming timed out after {timeout_seconds} seconds")
                                    yield "<<<TIMEOUT_ERROR>>>"
                                    break
                                    
                                if hasattr(chunk.choices[0], 'delta') and hasattr(chunk.choices[0].delta, 'content'):
                                    content = chunk.choices[0].delta.content
                                    if content:
                                        yield content
                        except Exception as stream_error:
                            print(f"Error during streaming: {stream_error}")
                            yield f"Error: {str(stream_error)}"
                    
                    print("Returning stream generator")
                    return process_stream()
                    
                except Exception as stream_error:
                    print(f"Stream setup error: {str(stream_error)}")
                    # Fall back to non-streaming on error
                    print("Falling back to non-streaming mode")
                    streaming = False
            
            # Non-streaming mode (either by choice or as fallback)
            if not streaming:
                try:
                    print(f"Making Groq API non-streaming call for slide {slide_num}")
                    response = client.chat.completions.create(
                        model="llama-3.1-8b-instant",
                        messages=messages,
                        temperature=0.3,
                        max_tokens=500,
                        timeout=8.0  # Set an explicit timeout for the API call
                    )
                    
                    summary = response.choices[0].message.content
                    print(f"Received non-streaming summary: {summary[:50]}...")
                    return summary
                except Exception as api_error:
                    print(f"API error in non-streaming mode: {str(api_error)}")
                    return generate_basic_summary(slide_text, slide_num)
        except (socket.timeout, TimeoutError) as timeout_error:
            print(f"Connection timed out while creating client: {str(timeout_error)}")
            return generate_basic_summary(slide_text, slide_num)
        except Exception as e:
            print(f"Error setting up Groq client: {str(e)}")
            return generate_basic_summary(slide_text, slide_num)
        finally:
            # Restore original socket timeout
            socket.setdefaulttimeout(original_timeout)
                
    except Exception as e:
        print(f"Uncaught error in generate_groq_summary: {str(e)}")
        # Return a basic summary as fallback
        return generate_basic_summary(slide_text, slide_num)

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
    
    # Clean up the text
    text = slide_text.strip()
    
    # Check if this mentions OCR unavailability
    ocr_unavailable = "[This slide appears to be primarily visual with limited text. OCR is not available" in text
    
    # Special handling for slides that need OCR but it's not available
    if ocr_unavailable:
        return f"**Visual Content** - This slide appears to primarily contain visual elements (images, charts, or diagrams). Limited text was detected as OCR functionality is not available in this environment. The visual elements likely illustrate important concepts from the presentation."
    
    # Create a very simple summary for other slides
    if len(text) < 100:
        # For very short text, return it directly
        return f"Slide {slide_num} contains: {text}"
    
    # For longer text, extract key sentences
    sentences = text.replace('\n', ' ').split('.')
    sentences = [s.strip() for s in sentences if s.strip()]
    
    # Take first, middle and last sentence if available
    summary_sentences = []
    if sentences:
        summary_sentences.append(sentences[0])  # First sentence
        
        if len(sentences) > 2:
            middle_idx = len(sentences) // 2
            summary_sentences.append(sentences[middle_idx])  # Middle sentence
            
        if len(sentences) > 1:
            summary_sentences.append(sentences[-1])  # Last sentence
    
    # Join the selected sentences
    if summary_sentences:
        summary = ". ".join(summary_sentences) + "."
        return summary
    else:
        return f"Slide {slide_num} contains text that could not be summarized: {text[:100]}..."

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

# Function to generate a summary locally when API fails
def generate_local_summary(slide_text, slide_num):
    """Generate a simple local summary when API is unavailable"""
    if is_title_slide(slide_text):
        return "**Title Slide** - This appears to be a title slide."
        
    words = slide_text.split()
    
    # Check if this slide contains OCR-extracted text
    has_ocr = "[OCR-extracted text:]" in slide_text
    
    # Check for image metadata we added in extract_text_from_pdf
    has_image_metadata = "[This slide appears to be primarily visual" in slide_text or "[Contains a visual element of size" in slide_text
    has_embedded_images = "[Contains" in slide_text and "embedded image(s)" in slide_text
    
    # If we have OCR text, create a summary focused on that
    if has_ocr:
        # Extract the OCR text section
        ocr_parts = slide_text.split("[OCR-extracted text:]")
        if len(ocr_parts) > 1:
            ocr_text = ocr_parts[1].strip()
            original_text = ocr_parts[0].strip()
            
            # Prepare a combined summary
            if len(original_text.split()) > 5:
                # If there was meaningful original text too
                return f"**Visual Slide with Text** - This slide contains both regular text and text extracted from images via OCR. Key content includes: **{' '.join(original_text.split()[:15])}... {' '.join(ocr_text.split()[:15])}...**"
            else:
                # If original text was minimal
                return f"**Visual Slide with OCR** - Text extracted from image content: **{' '.join(ocr_text.split()[:30])}...**"
    
    # Check if slide has minimal text but likely contains images
    if len(words) <= 10 or has_image_metadata:
        # For slides with very little text or known image content
        if has_image_metadata or has_embedded_images:
            # Extract image dimensions from our metadata
            image_info = ""
            dim_match = re.search(r'Contains a visual element of size (\d+)x(\d+)px', slide_text)
            if not dim_match:
                dim_match = re.search(r'Contains an image of size (\d+)x(\d+)px', slide_text)
            
            if dim_match:
                width, height = dim_match.groups()
                image_info = f" The image dimensions are {width}x{height} pixels."
            
            # Extract embedded image count if available    
            image_count = ""
            count_match = re.search(r'Contains (\d+) embedded image\(s\)', slide_text)
            if count_match:
                count = count_match.group(1)
                image_count = f" The slide contains {count} embedded image(s)."
            
            # Create enhanced summary for visual slides
            visible_text = []
            for w in words[:20]:
                if not w.startswith('[') and not w.startswith('Slide') and not w == ']':
                    visible_text.append(w)
            
            text_sample = ' '.join(visible_text).strip()
            if text_sample:
                return f"**Visual Slide with Text Elements** - This slide contains visual content with some text elements.{image_info}{image_count} The key text includes: **{text_sample}**"
            else:
                return f"**Visual Content** - This slide appears to contain primarily visual elements such as images, diagrams, charts, or graphs.{image_info}{image_count} The visual elements likely illustrate important concepts from the presentation."
        else:
            # For slides with just minimal text
            return f"**Slide with Minimal Text** - This slide contains {len(words)} words and may focus on key points or contain visual elements. Text includes: **{' '.join(words)}**"
            
    # For slides with substantial text content
    if len(words) <= 30:
        # Short text slide - include all content
        return f"**Concise Content Slide** - This slide presents key information in {len(words)} words: **{' '.join(words)}**"
    else:
        # Extract key sentences from longer text
        sentences = re.split(r'[.!?]+', slide_text)
        filtered_sentences = []
        
        # Process each sentence to extract meaningful ones
        for sentence in sentences:
            sentence = sentence.strip()
            # Skip short or meaningless sentences
            if len(sentence.split()) > 3 and not sentence.startswith('[') and not sentence.startswith('Slide'):
                filtered_sentences.append(sentence)
                if len(filtered_sentences) >= 3:  # Limit to ~3 key sentences
                    break
        
        # Create a summary based on key sentences
        if filtered_sentences:
            key_points = '. '.join(filtered_sentences)
            return f"**Content Slide** - Key points: **{key_points}**."
        else:
            # Fallback if sentence extraction fails
            word_sample = ' '.join(words[:30])
            return f"**Content Slide** - This slide contains {len(words)} words. Beginning with: **{word_sample}...**"

def get_slide_content(session_id, slide_num):
    """
    Get the content of a specific slide for direct reference
    
    Args:
        session_id (str): The session ID
        slide_num (int or str): The slide number
        
    Returns:
        tuple: (slide_text, exists) where exists is a boolean indicating if the slide exists
    """
    try:
        # Convert slide_num to string for consistent lookup
        str_slide_num = str(slide_num)
        
        # Get slide data
        slide_data = app.config.get('SLIDE_DATA', {}).get(session_id, {})
        if not slide_data:
            print(f"No data found for session {session_id}")
            return "", False
            
        # Get slide texts
        extraction_data = slide_data.get('extraction_data', {})
        if not extraction_data:
            print(f"No extraction data found for session {session_id}")
            return "", False
            
        slide_texts = extraction_data.get('slide_texts', {})
        if not slide_texts:
            print(f"No slide texts found for session {session_id}")
            return "", False
            
        # Check if the slide exists
        if str_slide_num in slide_texts:
            return slide_texts[str_slide_num], True
        else:
            # Get total slide count for error messaging
            total_slides = len(slide_texts)
            print(f"Slide {slide_num} not found. Available slides: 1-{total_slides}")
            return "", False
            
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
            {"role": "system", "content": """You are a helpful assistant answering questions about presentation slides. Your answers must be direct, concise, and contain ONLY the final answer with NO thinking process or meta-commentary. Never mention how you're approaching the answer.

When a user asks about a specific slide (e.g., "explain slide 9"), you should:
1. Focus primarily on that slide's content
2. If the slide doesn't exist in the presentation, clearly state this fact
3. Only provide information from other slides when it directly helps answer the question

For code-related questions:
1. Explain the code's purpose and functionality clearly
2. Highlight key components and their interactions
3. Provide concrete examples of what the code does when possible"""}
        ]
        
        # Handle slide-specific queries
        slide_query_match = re.search(r'(?:explain|show|tell me about|what is in|describe|summarize|content of)\s+slide\s+(\d+)(?:\s+|$|\?)', user_message.lower())
        specific_slide = None
        slide_content = ""
        slide_exists = False
        
        if slide_query_match:
            specific_slide = int(slide_query_match.group(1))
            print(f"Detected request for specific slide: {specific_slide}")
            
            # Get the specific slide content
            slide_content, slide_exists = get_slide_content(session_id, specific_slide)
            
            if slide_exists:
                # Prioritize the specific slide
                context_message = f"The user is asking about Slide {specific_slide}. Here is the content of that slide:\n\n{slide_content}"
                messages.append({"role": "system", "content": context_message})
                
                # Set current_slide to ensure we bias RAG towards this slide
                current_slide = specific_slide
            else:
                # Slide doesn't exist, but we'll still try RAG for related content
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
        api_key = os.environ.get("GROQ_API_KEY", "")
        
        # Check if API key is available
        if not api_key:
            print("No Groq API key found, using local response")
            if specific_slide and not slide_exists:
                return format_missing_slide_message(session_id, specific_slide, None, is_error=True)
            fallback_msg = "I'm sorry, but I don't have enough information to answer that question."
            if relevant_slides:
                fallback_msg += f" Your question appears to be about slides: {', '.join([str(num) for num in relevant_slides])}"
            return fallback_msg
        
        # Set a quick timeout for faster fallback if API is unresponsive
        import socket
        original_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(5.0)  # 5 second timeout
        
        try:
            # Create the client
            print("Creating Groq client for chat")
            client = Groq(api_key=api_key)
            
            # Estimate token count for the request (rough estimation using 4 chars per token)
            base_tokens = 150  # For system message
            context_tokens = len(context) // 4 if context else 0
            user_tokens = len(user_message) // 4
            est_prompt_tokens = base_tokens + context_tokens + user_tokens
            est_completion_tokens = 300  # Estimate for completion
            est_total_tokens = est_prompt_tokens + est_completion_tokens
            
            print(f"Estimated token usage for chat: {est_total_tokens} tokens")
            
            try:
                print("Making Groq API call for chat response")
                completion = client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=messages,
                    temperature=0.1,  # Very low temperature for more focused responses
                    max_tokens=500,  # Reduced from 700 to save tokens
                    timeout=8.0  # Set an explicit timeout for the API call
                )
                
                # Extract content from the response with safer access
                if hasattr(completion, 'choices') and completion.choices and completion.choices[0] and hasattr(completion.choices[0], 'message'):
                    content = completion.choices[0].message.content
                    
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
                
        except (socket.timeout, TimeoutError) as timeout_error:
            print(f"Connection timed out while creating client: {str(timeout_error)}")
            fallback_msg = "I'm sorry, but the AI service is not responding at the moment."
            if relevant_slides:
                fallback_msg += f" Your question appears to be about slides: {', '.join([str(num) for num in relevant_slides])}"
            return fallback_msg
            
        except Exception as e:
            print(f"Error setting up Groq client: {str(e)}")
            fallback_msg = "I'm sorry, but there was a problem connecting to the AI service."
            if relevant_slides:
                fallback_msg += f" Your question appears to be about slides: {', '.join([str(num) for num in relevant_slides])}"
            return fallback_msg
            
        finally:
            # Restore original socket timeout
            socket.setdefaulttimeout(original_timeout)
                
    except Exception as e:
        print(f"Uncaught error in generate_groq_chat_response: {str(e)}")
        return f"I apologize, but I couldn't process your request. Error: {str(e)}"

@app.route('/')
def index():
    """Render the landing page"""
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', 'Landing.html')
    if os.path.exists(template_path):
        print(f"Landing page template found at: {template_path}")
    else:
        print(f"WARNING: Landing page template NOT found at expected path: {template_path}")
        # List templates directory contents for debugging
        templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
        if os.path.exists(templates_dir):
            print(f"Templates directory contents: {os.listdir(templates_dir)}")
        else:
            print(f"Templates directory not found at: {templates_dir}")
    
    return render_template('Landing.html')

@app.route('/health')
def health():
    """Health check endpoint for Google Cloud Run"""
    # Detailed platform information
    platform_info = {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version()
    }
    
    # Check Tesseract path
    tesseract_path = "System PATH"
    if platform.system() == 'Windows':
        tesseract_path = pytesseract.pytesseract.tesseract_cmd
    
    return jsonify({
        "status": "healthy",
        "timestamp": str(datetime.datetime.now()),
        "service": "Sumora AI",
        "platform": platform_info,
        "tesseract": {
            "available": tesseract_available,
            "path": tesseract_path
        },
        "groq_available": groq_available
    })

@app.route('/app')
def app_index():
    """Render the main application page"""
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_slides():
    """Handle slide upload and processing"""
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    # Check if the file is a PDF
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Only PDF files are supported'}), 400
    
    try:
        # Create a unique session ID if not provided
        session_id = str(uuid.uuid4())
        
        # Set as the active session for convenience in development
        global active_session_id
        active_session_id = session_id
    
        # Save the uploaded file
        filename = secure_filename(file.filename)
        upload_dir = os.path.join(tempfile.gettempdir(), 'slide_uploads')
        os.makedirs(upload_dir, exist_ok=True)
        file_path = os.path.join(upload_dir, f"{session_id}_{filename}")
        file.save(file_path)
    
        # Process the PDF in the background
        print(f"Processing PDF: {file_path}")
        
        # Extract text and render slide images
        text_content, slide_images = extract_text_from_pdf(file_path, session_id)
        
        # Create or update the chat session
        get_or_create_chat_session(session_id)
        
        # Initialize the SLIDE_DATA structure for this session
        app.config['SLIDE_DATA'][session_id] = {
            'extraction_data': {
                'slide_texts': slide_contents_structured.get(session_id, {}),
            },
            'slide_summaries': {},
            'slide_titles': {}
        }
        
        # Count total slides
        slide_pattern = re.compile(r"Slide\s+(\d+):")
        slide_matches = slide_pattern.findall(text_content)
        total_slides = len(slide_matches) if slide_matches else len(slide_images)
        
        # Start background thread to generate summaries for all slides
        import threading
        summary_thread = threading.Thread(
            target=generate_all_summaries_background,
            args=(session_id,)
        )
        summary_thread.daemon = True
        summary_thread.start()
        
        # Return success with session ID and total slides
        return jsonify({
            'success': True, 
            'message': 'File uploaded and processed successfully',
            'session_id': session_id,
            'total_slides': total_slides
        })
    
    except Exception as e:
        print(f"Error processing uploaded file: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/chat', methods=['POST'])
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
            
        # Collect slides that actually need processing
        slides_to_process = []
        valid_slide_nums = []
        
        for slide_num in slide_nums:
            str_slide_num = str(slide_num)
            # Check if the slide exists
            if str_slide_num not in slide_texts:
                print(f"Slide {slide_num} (key={str_slide_num}) not found in slide_texts. Available keys: {list(slide_texts.keys())}")
                continue
                
            # Skip empty slides
            slide_text = slide_texts.get(str_slide_num, "")
            if not slide_text.strip():
                print(f"Slide {slide_num} is empty, skipping")
                continue
                
            # Check if we need to process this slide
            if force_regenerate or str_slide_num not in slide_data['slide_summaries']:
                slides_to_process.append(slide_num)
            
            valid_slide_nums.append(slide_num)
            
        # If we don't need to process any slides, return cached results
        if not slides_to_process:
            result = {}
            for slide_num in valid_slide_nums:
                str_slide_num = str(slide_num)
                if str_slide_num in slide_data['slide_summaries']:
                    result[slide_num] = slide_data['slide_summaries'][str_slide_num]
            print(f"Returning cached summaries for {len(result)} slides")
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
                
        # Process each slide
        results = {}
        
        for slide_num in valid_slide_nums:
            str_slide_num = str(slide_num)
            
            # Use cached summary if available and not forcing regeneration
            if str_slide_num in slide_data['slide_summaries'] and not force_regenerate:
                results[slide_num] = slide_data['slide_summaries'][str_slide_num]
                continue
                
            # Skip if slide doesn't exist in text data
            if str_slide_num not in slide_texts:
                continue
                
            # Get the slide text
            slide_text = slide_texts.get(str_slide_num, "")
            if not slide_text.strip():
                continue
                
            # Get surrounding slides for context
            surrounding_slides = {}
            nearby_slides = [slide_num - 1, slide_num + 1]
            for nearby_num in nearby_slides:
                if str(nearby_num) in slide_texts:
                    neighboring_text = slide_texts[str(nearby_num)]
                    # Limit context size
                    if len(neighboring_text) > 300:
                        words = neighboring_text.split()
                        neighboring_text = " ".join(words[:30]) + "..."
                    surrounding_slides[nearby_num] = neighboring_text
            
            try:
                # Generate summary without streaming for batch efficiency
                summary = generate_groq_summary(
                    slide_text=slide_text,
                    slide_num=slide_num,
                    streaming=False
                )
                
                # Store the summary
                slide_data['slide_summaries'][str_slide_num] = summary
                results[slide_num] = summary
                
            except Exception as e:
                print(f"Error generating summary for slide {slide_num}: {str(e)}")
                # Use basic fallback summary
                basic_summary = generate_basic_summary(slide_text, slide_num)
                slide_data['slide_summaries'][str_slide_num] = basic_summary
                results[slide_num] = basic_summary
                
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
    
    # Get the slide text
    slide_text = slide_texts.get(str_slide_num, "")
    if not slide_text.strip():
        print(f"Slide {slide_num} is empty")
        yield json.dumps({"error": f"Slide {slide_num} is empty"})
        return
    
    # Initialize slide_summaries if not present
    if 'slide_summaries' not in slide_data:
        slide_data['slide_summaries'] = {}
    
    # Check if we already have the summary cached and force_regenerate is False
    if str_slide_num in slide_data['slide_summaries'] and not force_regenerate:
        cached_summary = slide_data['slide_summaries'][str_slide_num]
        print(f"Using cached summary for slide {slide_num}")
        
        # First yield the title
        title = extract_slide_title(slide_text, slide_num)
        yield json.dumps({"title": title})
        
        # Then yield the summary
        yield json.dumps({"summary": cached_summary})
        yield json.dumps({"complete": True})
        return
    
    # Yield progress update
    yield json.dumps({"progress": "Generating summary..."})
    
    # Use a simplified approach - just generate the summary directly
    # Extract title from the slide text
    title = extract_slide_title(slide_text, slide_num)
    yield json.dumps({"title": title})
    
    try:
        print(f"Generating summary for slide {slide_num}")
        # Try to generate summary with streaming first
        completion_stream = generate_groq_summary(
            slide_text=slide_text,
            slide_num=slide_num,
            streaming=True
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
                streaming=False
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
    api_key = os.environ.get("GROQ_API_KEY", "")
    
    # Check if API key is available
    if not api_key:
        print("No Groq API key found, using local overview")
        return toc_overview
    
    # Set a quick timeout for faster fallback if API is unresponsive
    import socket
    original_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(5.0)  # 5 second timeout
    
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
            client = Groq(api_key=api_key)
            
            try:
                # API call with minimal tokens and timeout
                print("Making Groq API call for presentation overview")
                response = client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=messages,
                    temperature=0.1,
                    max_tokens=250,  # Minimal tokens for an overview
                    timeout=8.0  # Set an explicit timeout for the API call
                )
                
                # Get overview content
                if hasattr(response, 'choices') and response.choices and hasattr(response.choices[0], 'message'):
                    overview = response.choices[0].message.content
                    
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
                
        except (socket.timeout, TimeoutError) as timeout_error:
            print(f"Connection timed out while creating client: {str(timeout_error)}")
            return toc_overview
            
        except Exception as e:
            print(f"Error setting up Groq client: {str(e)}")
            return toc_overview
            
    except Exception as e:
        print(f"Uncaught error in generate_presentation_overview: {str(e)}")
        # Always return the fallback TOC if API call fails
        return toc_overview
    finally:
        # Restore original socket timeout
        socket.setdefaulttimeout(original_timeout)

# Add this function after the existing functions
def generate_all_summaries_background(session_id):
    """
    Generate summaries for all slides in the background.
    This avoids making the user wait for all summaries to be generated during upload.
    
    Args:
        session_id (str): The session ID to generate summaries for
    """
    print(f"Starting background generation of summaries for session {session_id}")
    
    # Get slide data
    slide_data = app.config.get('SLIDE_DATA', {}).get(session_id, {})
    if not slide_data:
        print(f"No data found for session {session_id}")
        return
        
    # Get slide texts
    extraction_data = slide_data.get('extraction_data', {})
    if not extraction_data:
        print(f"No extraction data found for session {session_id}")
        return
        
    slide_texts = extraction_data.get('slide_texts', {})
    if not slide_texts:
        print(f"No slide texts found for session {session_id}")
        return
        
    # Initialize slide_summaries if not present
    if 'slide_summaries' not in slide_data:
        slide_data['slide_summaries'] = {}
        
    # Generate presentation overview once for efficiency
    presentation_overview = None
    try:
        presentation_overview = generate_presentation_overview(session_id)
    except Exception as e:
        print(f"Error generating presentation overview: {str(e)}")
        # Continue without overview
        
    # Process each slide
    print(f"Generating summaries for {len(slide_texts)} slides in session {session_id}")
    
    for slide_num_str, slide_text in slide_texts.items():
        # Skip if already generated
        if slide_num_str in slide_data['slide_summaries']:
            continue
            
        # Skip empty slides
        if not slide_text.strip():
            continue
            
        slide_num = int(slide_num_str)
        print(f"Generating summary for slide {slide_num}")
        
        try:
            # Generate summary without streaming for efficiency
            summary = generate_groq_summary(
                slide_text=slide_text,
                slide_num=slide_num,
                streaming=False
            )
            
            # Store the summary
            slide_data['slide_summaries'][slide_num_str] = summary
            print(f"Successfully generated summary for slide {slide_num}")
            
        except Exception as e:
            print(f"Error generating summary for slide {slide_num}: {str(e)}")
            # Use basic fallback summary
            basic_summary = generate_basic_summary(slide_text, slide_num)
            slide_data['slide_summaries'][slide_num_str] = basic_summary
            
    print(f"Completed background generation of summaries for session {session_id}")

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

# Run the app
if __name__ == '__main__':
    # For Cloud Run, use a simpler startup approach focused on reliability
    try:
        port = int(os.environ.get('PORT', 8080))
        print(f"Starting application on port {port}")
        print(f"Running in {'Production' if 'K_SERVICE' in os.environ else 'Development'} mode")
        
        # Simple but reliable startup - just run with the right host and port
        app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
    except Exception as e:
        print(f"ERROR STARTING APPLICATION: {str(e)}")
        import traceback
        traceback.print_exc()
        raise
