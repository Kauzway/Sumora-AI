// Variables to store session information
let sessionId = null;
let totalSlides = 0;
let currentSlideIndex = 0;
let slideData = []; // Will store all slide data including summaries, titles, and image paths
let isDarkMode = false; // Track dark mode state
let isSummaryLoading = false; // Track if summaries are being loaded
let summaryLoadingTimers = {}; // For token streaming simulation

// DOM elements
const uploadForm = document.getElementById('upload-form');
const uploadStatus = document.getElementById('upload-status');
const uploadSection = document.getElementById('upload-section');
const resultsSection = document.getElementById('results-section');
const slideSummaries = document.getElementById('slide-summaries');
const chatMessages = document.getElementById('chat-messages');
const userInput = document.getElementById('user-input');
const sendButton = document.getElementById('send-btn');
const backToUploadBtn = document.getElementById('back-to-upload-btn');
const prevSlideBtn = document.getElementById('prev-slide');
const nextSlideBtn = document.getElementById('next-slide');
const currentSlideElem = document.getElementById('current-slide');
const slideThumbnails = document.getElementById('slide-thumbnails');
const slideTitle = document.getElementById('slide-title');
const slideSummary = document.getElementById('slide-summary');
const currentSlideNum = document.getElementById('current-slide-num');
const totalSlidesElem = document.getElementById('total-slides');
const gotoSlideInput = document.getElementById('goto-slide');
const gotoBtn = document.getElementById('goto-btn');
const fileInput = document.getElementById('slide-file');
const fileNameDisplay = document.getElementById('file-name');
const themeToggle = document.getElementById('theme-toggle');
const slideRangeStart = document.getElementById('slide-range-start');
const slideRangeEnd = document.getElementById('slide-range-end');
const applyRangeBtn = document.getElementById('apply-range-btn');

// Initialize dark mode from localStorage if available
function initTheme() {
    const savedTheme = localStorage.getItem('darkMode');
    if (savedTheme === 'true') {
        document.body.classList.add('dark-mode');
        themeToggle.classList.add('active');
        themeToggle.querySelector('.toggle-handle i').className = 'fas fa-moon';
        isDarkMode = true;
    }
}

// Initialize theme when page loads
initTheme();

// Theme toggle functionality
themeToggle.addEventListener('click', function() {
    isDarkMode = !isDarkMode;
    document.body.classList.toggle('dark-mode');
    themeToggle.classList.toggle('active');
    
    // Change icon
    const icon = themeToggle.querySelector('.toggle-handle i');
    icon.className = isDarkMode ? 'fas fa-moon' : 'fas fa-sun';
    
    // Save preference
    localStorage.setItem('darkMode', isDarkMode);
});

// Show file name when selected
fileInput.addEventListener('change', function() {
    if (fileInput.files.length > 0) {
        fileNameDisplay.textContent = `Selected: ${fileInput.files[0].name}`;
    } else {
        fileNameDisplay.textContent = '';
    }
});

// Handle file upload
uploadForm.addEventListener('submit', function(e) {
    e.preventDefault();
    
    const file = fileInput.files[0];
    
    if (!file) {
        showUploadStatus('Please select a file to upload.', 'alert-warning');
        return;
    }
    
    // Check file type - only accept PDF files
    const fileType = file.name.split('.').pop().toLowerCase();
    if (fileType !== 'pdf') {
        showUploadStatus('Only PDF files are supported.', 'alert-danger');
        return;
    }
    
    // Show loading status
    showUploadStatus('Uploading and processing your slides...', 'alert-info');
    
    // Create form data for file upload
    const formData = new FormData();
    formData.append('file', file);
    
    // Log upload attempt
    console.log("Attempting to upload file:", file.name, "of type:", file.type, "size:", file.size, "bytes");
    
    // Use XMLHttpRequest instead of fetch for better compatibility
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload', true);
    
    xhr.onload = function() {
        if (xhr.status === 200) {
            try {
                const response = JSON.parse(xhr.responseText);
                console.log("Upload successful:", response);
                
                // Store session ID
                sessionId = response.session_id;
                
                // Show success message
                showUploadStatus('Slides processed successfully!', 'alert-success');
                
                // Show results section and hide upload section IMMEDIATELY
                uploadSection.style.display = 'none';
                resultsSection.style.display = 'block';
                
                // Create placeholder slides immediately for a faster UI experience
                createInitialPlaceholders(response.total_images || 10);
                
                // Load slides UI immediately (sets up navigation and event listeners)
                initSlideViewerUI();
                
                // Then fetch slide data (images first, then summaries) in the background
                fetchSlideImages()
                  .then(() => fetchSlideSummaries());
                
                // Focus on input field
                userInput.focus();
            } catch (error) {
                console.error("Error parsing response:", error);
                showUploadStatus('Error processing server response.', 'alert-danger');
            }
        } else {
            console.error("Upload failed with status:", xhr.status);
            let errorMsg = 'Upload failed';
            
            try {
                const response = JSON.parse(xhr.responseText);
                if (response && response.error) {
                    errorMsg = response.error;
                }
            } catch (e) {
                // If parsing fails, use the default error message
            }
            
            showUploadStatus(`Error: ${errorMsg}`, 'alert-danger');
        }
    };
    
    xhr.onerror = function() {
        console.error("Network error during upload");
        showUploadStatus('Network error during upload. Please try again.', 'alert-danger');
    };
    
    xhr.upload.onprogress = function(event) {
        if (event.lengthComputable) {
            const percentComplete = (event.loaded / event.total) * 100;
            console.log(`Upload progress: ${percentComplete.toFixed(2)}%`);
            showUploadStatus(`Uploading: ${percentComplete.toFixed(0)}%`, 'alert-info');
        }
    };
    
    xhr.send(formData);
});

// Create initial slide placeholders immediately after upload
function createInitialPlaceholders(estimatedSlideCount) {
    // Clear any existing data
    slideData = [];
    
    // Create placeholders for the estimated number of slides
    for (let i = 0; i < estimatedSlideCount; i++) {
        slideData.push({
            slideNumber: i + 1,
            title: `Slide ${i + 1}`,
            summary: "Loading summary...",
            imagePath: null, // No image yet, will be populated later
            isLoading: true  // Flag to indicate this is a placeholder
        });
    }
    
    // Create thumbnails with placeholders
    createThumbnails();
    
    // Show the first slide placeholder
    showSlide(0);
    
    // Update slide count indicator (will be updated with accurate count later)
    totalSlides = estimatedSlideCount;
    totalSlidesElem.textContent = `of ${totalSlides}`;
}

// Fetch slide images separately for faster initial display
async function fetchSlideImages() {
    if (!sessionId) {
        console.error("No active session");
        return;
    }
    
    try {
        const response = await fetch('/get_slide_images', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                session_id: sessionId
            })
        });
        
        const data = await response.json();
        
        if (data.error) {
            console.error("Error fetching slide images:", data.error);
            return;
        }
        
        // Update the total slide count
        totalSlides = data.total_slides || slideData.length;
        totalSlidesElem.textContent = `of ${totalSlides}`;
        
        // Update the range selector with total slides
        slideRangeEnd.max = totalSlides;
        slideRangeEnd.value = Math.min(Math.max(10, totalSlides), totalSlides);
        slideRangeStart.max = totalSlides;
        gotoSlideInput.max = totalSlides;
        
        // Update our existing placeholder slideData with real image paths
        data.slide_image_paths.forEach((imagePath, index) => {
            if (index < slideData.length) {
                slideData[index].imagePath = imagePath;
            } else {
                // Add new slides if we have more images than placeholders
                slideData.push({
                    slideNumber: index + 1,
                    title: `Slide ${index + 1}`,
                    summary: "Loading summary...",
                    imagePath: imagePath,
                    isLoading: true
                });
            }
        });
        
        // Update the UI with new image data
        createThumbnails();
        showSlide(currentSlideIndex); // Refresh current slide view
        
    } catch (error) {
        console.error("Error fetching slide images:", error);
    }
}

// Fetch summaries for slides in smaller batches with streaming updates
async function fetchSlideSummaries() {
    if (!sessionId) {
        console.error("No active session");
        return;
    }
    
    // Mark that we're loading summaries
    isSummaryLoading = true;
    
    try {
        // Get summaries in batches to show progress
        const batchSize = 5; // Process 5 slides at a time
        const startSlide = 1;
        const numBatches = Math.ceil(totalSlides / batchSize);
        
        for (let batch = 0; batch < numBatches; batch++) {
            const batchStart = startSlide + (batch * batchSize);
            const batchEnd = Math.min(batchStart + batchSize - 1, totalSlides);
            
            console.log(`Fetching summary batch ${batch + 1}/${numBatches}: slides ${batchStart}-${batchEnd}`);
            
            // Create array of slide numbers for this batch
            const slideNumsArray = [];
            for (let i = batchStart; i <= batchEnd; i++) {
                slideNumsArray.push(i);
            }
            
            // Create comma-separated list of slide numbers
            const slideNums = slideNumsArray.join(',');
            
            // Use GET request with query parameters
            const url = `/get_summaries?session_id=${sessionId}&slide_nums=${slideNums}&force_regenerate=false`;
            
            const response = await fetch(url, {
                method: 'GET',
                headers: {
                    'Content-Type': 'application/json',
                }
            });
            
            if (!response.ok) {
                console.error(`Error fetching summaries: ${response.status} ${response.statusText}`);
                continue; // Try the next batch
            }
            
            const data = await response.json();
            
            if (data.error) {
                console.error(`Error fetching summary batch ${batch + 1}:`, data.error);
                continue; // Try the next batch
            }
            
            // Update slideData with new summaries
            for (const [slideNum, summary] of Object.entries(data)) {
                const slideIndex = parseInt(slideNum) - 1;
                if (slideIndex >= 0 && slideIndex < slideData.length) {
                    slideData[slideIndex].summary = summary;
                    slideData[slideIndex].isLoading = false;
                }
            }
            
            // Update UI if the current slide is in this batch
            if (currentSlideIndex >= batchStart - 1 && currentSlideIndex <= batchEnd - 1) {
                updateSlideSummary(slideData[currentSlideIndex]);
            }
        }
        
        // Ensure all slides have at least a basic summary
        slideData.forEach((slide, index) => {
            if (!slide.summary || slide.summary === "Loading summary...") {
                slide.summary = `**Basic Summary** - This is slide ${slide.slideNumber} of the presentation. A detailed summary couldn't be generated automatically.`;
                slide.isLoading = false;
            }
        });
        
        // Final UI update
        createThumbnails();
        showSlide(currentSlideIndex); // Refresh current slide view
        
        // Mark that we're done loading summaries
        isSummaryLoading = false;
        
    } catch (error) {
        console.error("Error fetching slide summaries:", error);
        
        // Ensure all slides have at least a basic summary even if there was an error
        slideData.forEach((slide, index) => {
            if (!slide.summary || slide.summary === "Loading summary...") {
                slide.summary = `**Basic Summary** - This is slide ${slide.slideNumber} of the presentation. A detailed summary couldn't be generated due to an error.`;
                slide.isLoading = false;
            }
        });
        
        // Final UI update
        showSlide(currentSlideIndex);
        isSummaryLoading = false;
    }
}

// Modified fetchAllSlideSummaries to use our new approach
async function fetchAllSlideSummaries() {
    try {
        // Get initial count and image data
        await fetchSlideImages();
        
        // Then get summaries in batches with streaming updates
        await fetchSlideSummaries();
        
    } catch (error) {
        console.error("Error in fetchAllSlideSummaries:", error);
        currentSlideElem.innerHTML = `<div class="alert alert-danger text-center">Error loading slides: ${error.message}</div>`;
        isSummaryLoading = false;
    }
}

// Initialize slide viewer UI immediately after upload
// This function sets up the UI without waiting for summaries
function initSlideViewerUI() {
    // Set loading states for slides and summaries
    currentSlideElem.innerHTML = '<div class="d-flex align-items-center justify-content-center h-100"><div class="spinner-border text-primary" role="status"></div><p class="mt-3">Loading slides...</p></div>';
    slideSummary.innerHTML = '<div class="d-flex align-items-center justify-content-center"><div class="spinner-border text-primary" role="status"></div><p class="mt-2">Generating summary...</p></div>';
    slideThumbnails.innerHTML = '<div class="text-center w-100"><div class="spinner-border spinner-border-sm text-primary" role="status"></div> Loading thumbnails...</div>';
    
    // Set up event listeners for navigation
    prevSlideBtn.addEventListener('click', navigateToPrevSlide);
    nextSlideBtn.addEventListener('click', navigateToNextSlide);
    
    // Set up goto slide functionality
    gotoBtn.addEventListener('click', goToSpecificSlide);
    gotoSlideInput.addEventListener('keypress', function(e) {
        if (e.key === 'Enter') {
            goToSpecificSlide();
        }
    });
    
    // Add keyboard navigation
    document.addEventListener('keydown', handleKeyNavigation);
}

// Function to navigate to a specific slide number
function goToSpecificSlide() {
    const slideNum = parseInt(gotoSlideInput.value);
    if (slideNum && slideNum >= 1 && slideNum <= totalSlides) {
        // Check if this slide is in our current loaded range
        const slideIndex = slideData.findIndex(slide => slide.slideNumber === slideNum);
        
        if (slideIndex !== -1) {
            // Slide is in our current range, just navigate to it
            showSlide(slideIndex);
        } else {
            // Slide is not in current range, fetch a new range centered on this slide
            const start = Math.max(1, slideNum - 4);  // Try to center the requested slide
            const end = Math.min(totalSlides, start + 9);
            
            // Update range selector inputs
            slideRangeStart.value = start;
            slideRangeEnd.value = end;
            
            // Fetch the new range
            fetchSlideRange(start, end).then(() => {
                // After fetching, find the slide in the new data and show it
                const newIndex = slideData.findIndex(slide => slide.slideNumber === slideNum);
                if (newIndex !== -1) {
                    showSlide(newIndex);
                }
            });
        }
    }
}

// Function to show upload status with appropriate styling
function showUploadStatus(message, alertClass) {
    uploadStatus.textContent = message;
    uploadStatus.className = 'alert mt-4 text-center ' + alertClass;
    uploadStatus.style.display = 'block';
}

// Back to upload button
backToUploadBtn.addEventListener('click', function() {
    resultsSection.style.display = 'none';
    uploadSection.style.display = 'block';
    fileInput.value = ''; // Clear file input
    fileNameDisplay.textContent = '';
    sessionId = null;
    totalSlides = 0;
    currentSlideIndex = 0;
    slideData = [];
});

// Apply slide range selection
applyRangeBtn.addEventListener('click', function() {
    const start = parseInt(slideRangeStart.value);
    const end = parseInt(slideRangeEnd.value);
    
    if (isNaN(start) || isNaN(end) || start < 1 || end < start) {
        alert('Please enter a valid range of slides');
        return;
    }
    
    // Fetch the selected range
    fetchSlideRange(start, end);
});

// Fetch a specific range of slides
async function fetchSlideRange(startSlide, endSlide) {
    if (!sessionId) {
        console.error("No active session");
        return;
    }
    
    try {
        // Show loading state
        slideThumbnails.innerHTML = '<div class="text-center w-100 py-3"><div class="spinner-border text-primary" role="status"></div><p class="mt-2">Loading slides...</p></div>';
        currentSlideElem.innerHTML = '<div class="text-center p-5"><div class="spinner-border text-primary" role="status"></div><p class="mt-3">Loading slides...</p></div>';
        slideSummary.innerHTML = '<div class="text-center p-4"><div class="spinner-border text-primary" role="status"></div><p class="mt-3">Generating summary...</p></div>';
        
        console.log(`Fetching slide range: ${startSlide} to ${endSlide}`);
        
        const response = await fetch('/get_summaries', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                session_id: sessionId,
                start_slide: startSlide,
                end_slide: endSlide
            })
        });
        
        const data = await response.json();
        
        if (data.error) {
            console.error("Error fetching slide range:", data.error);
            slideThumbnails.innerHTML = `<div class="alert alert-danger w-100 text-center">Error: ${data.error}</div>`;
            return;
        }
        
        console.log("Received slide data:", data);
        
        // Clear existing slide data completely - prevents duplication
        slideData = [];
        
        // Create fresh slideData array from the response
        slideData = data.slide_indices.map((slideIndex, i) => {
            return {
                slideNumber: slideIndex,
                title: data.titles[i] || `Slide ${slideIndex}`,
                summary: data.summaries[i] || "No summary available for this slide.",
                imagePath: data.slide_image_paths[i] || null
            };
        });
        
        // Sort by slide number to ensure correct order
        slideData.sort((a, b) => a.slideNumber - b.slideNumber);
        
        // Update the UI
        createThumbnails();
        showSlide(0);  // Show the first slide in the new range
        
    } catch (error) {
        console.error("Error fetching slide range:", error);
        slideThumbnails.innerHTML = `<div class="alert alert-danger w-100 text-center">Error loading slides: ${error.message}</div>`;
    }
}

// Create thumbnails for all slides
function createThumbnails() {
    slideThumbnails.innerHTML = '';
    
    if (slideData.length === 0) {
        slideThumbnails.innerHTML = '<div class="alert alert-warning w-100 text-center">No slides available</div>';
        return;
    }
    
    slideData.forEach((slide, index) => {
        const thumbnail = document.createElement('div');
        thumbnail.className = 'thumbnail';
        thumbnail.dataset.index = index;
        
        if (slide.imagePath) {
            const img = document.createElement('img');
            img.src = slide.imagePath;
            img.alt = `Thumbnail for slide ${slide.slideNumber}`;
            img.loading = "lazy"; // Add lazy loading for better performance
            img.onerror = function() {
                thumbnail.innerHTML = `<div style="width:100%;height:100%;background:#eee;display:flex;align-items:center;justify-content:center;font-size:12px;">Slide ${slide.slideNumber}</div>`;
            };
            thumbnail.appendChild(img);
        } else {
            thumbnail.innerHTML = `<div style="width:100%;height:100%;background:#eee;display:flex;align-items:center;justify-content:center;font-size:12px;">Slide ${slide.slideNumber}</div>`;
        }
        
        thumbnail.addEventListener('click', () => {
            showSlide(index);
        });
        
        slideThumbnails.appendChild(thumbnail);
    });
}

// Navigation functions to keep them separate and avoid duplicates
function navigateToPrevSlide() {
    if (currentSlideIndex > 0) {
        showSlide(currentSlideIndex - 1);
    }
}

function navigateToNextSlide() {
    if (currentSlideIndex < slideData.length - 1) {
        showSlide(currentSlideIndex + 1);
    }
}

function handleKeyNavigation(e) {
    if (e.key === 'ArrowLeft') {
        navigateToPrevSlide();
    } else if (e.key === 'ArrowRight') {
        navigateToNextSlide();
    }
}

// Display the summary with a token streaming effect
function displaySummaryWithStreaming(summary, elementToUpdate) {
    // Clear any previous loading timers for this element
    const timerId = elementToUpdate.dataset.timerId;
    if (timerId) {
        clearInterval(parseInt(timerId));
    }
    
    // Start with empty content
    elementToUpdate.innerHTML = '';
    
    // Simulate streaming by revealing the content progressively
    let displayedLength = 0;
    const fullContent = `<p>${processMarkdown(summary)}</p>`;
    
    // For HTML content, we'll need to extract the text content first
    const tempDiv = document.createElement('div');
    tempDiv.innerHTML = fullContent;
    const textContent = tempDiv.textContent;
    
    // Create a timer that progressively reveals more of the content
    const charIncrement = 5; // Characters to add per tick
    
    const timer = setInterval(() => {
        if (displayedLength >= textContent.length) {
            // If we're done, show the full formatted content
            elementToUpdate.innerHTML = fullContent;
            clearInterval(timer);
        } else {
            // Increment the displayed length
            displayedLength += charIncrement;
            // Create a progress indicator
            const progress = Math.min(100, Math.round((displayedLength / textContent.length) * 100));
            // Show a placeholder with increasing content
            elementToUpdate.innerHTML = `<p>${processMarkdown(summary.substring(0, displayedLength))}<span class="cursor-blink">|</span></p>`;
        }
    }, 30); // Update every 30ms for a smooth effect
    
    // Store the timer ID for cleanup
    elementToUpdate.dataset.timerId = timer.toString();
}

// Show a specific slide
function showSlide(index) {
    if (!slideData || slideData.length === 0) {
        console.error("No slide data available");
        return;
    }
    
    // Ensure index is within bounds
    if (index < 0) index = 0;
    if (index >= slideData.length) index = slideData.length - 1;
    
    // Store current index
    currentSlideIndex = index;
    
    // Get the current slide data
    const slide = slideData[index];
    const slideNumber = slide.slideNumber;
    
    // Show slide number
    currentSlideNum.textContent = `Slide ${slideNumber}`;
    
    // Update thumbnail highlighting
    const thumbnails = document.querySelectorAll('.thumbnail');
    thumbnails.forEach(thumb => thumb.classList.remove('active'));
    const currentThumb = document.querySelector(`.thumbnail[data-index="${index}"]`);
    if (currentThumb) {
        currentThumb.classList.add('active');
        // Scroll the thumbnail into view
        currentThumb.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
    }
    
    // Enable/disable navigation buttons based on position
    prevSlideBtn.disabled = index === 0;
    nextSlideBtn.disabled = index === slideData.length - 1;
    
    // Update slide viewer with image
    updateSlideImage(slide);
    
    // Update summary panel
    updateSlideSummary(slide);
    
    // Update "Go to slide" input max value
    gotoSlideInput.max = slideData.length;
    
    // Update mobile indicators
    const mobileCurrentSlide = document.getElementById('mobile-current-slide');
    const mobileTotalSlides = document.getElementById('mobile-total-slides');
    if (mobileCurrentSlide) mobileCurrentSlide.textContent = slideNumber;
    if (mobileTotalSlides) mobileTotalSlides.textContent = `/ ${totalSlides}`;
}

// Separate function to update the slide image immediately
function updateSlideImage(slide) {
    const currentSlideElem = document.getElementById('current-slide');
    
    // Clear current content
    currentSlideElem.innerHTML = '';
    
    if (slide.imagePath) {
        // Create and show the image
        const img = document.createElement('img');
        img.src = slide.imagePath;
        img.alt = `Slide ${slide.slideNumber}`;
        img.className = 'slide-img';
        
        // Add loading indicator
        const loadingOverlay = document.createElement('div');
        loadingOverlay.className = 'loading-overlay';
        loadingOverlay.innerHTML = `
            <div class="spinner-border text-primary" role="status">
                <span class="visually-hidden">Loading image...</span>
            </div>
        `;
        
        // Add both to the slide container
        currentSlideElem.appendChild(img);
        currentSlideElem.appendChild(loadingOverlay);
        
        // Remove loading overlay when image loads
        img.onload = function() {
            loadingOverlay.remove();
        };
        
        // Handle image loading error
        img.onerror = function() {
            loadingOverlay.innerHTML = `
                <div class="text-center p-4">
                    <i class="fas fa-exclamation-triangle text-warning fs-1 mb-3"></i>
                    <p>Error loading image for Slide ${slide.slideNumber}</p>
                </div>
            `;
        };
    } else {
        // Show placeholder if no image
        currentSlideElem.innerHTML = `
            <div class="slide-placeholder d-flex align-items-center justify-content-center h-100">
                <div class="text-center">
                    <i class="fas fa-presentation fs-1 mb-3 text-secondary"></i>
                    <p>Slide ${slide.slideNumber}</p>
                    <p class="text-muted small">Image not available</p>
                </div>
            </div>
        `;
    }
}

// Separate function to handle summary streaming and updates
function updateSlideSummary(slide) {
    // Set the title immediately
    slideTitle.textContent = slide.title || `Slide ${slide.slideNumber}`;
    
    // If we already have a cached summary, show it immediately
    if (slide.summary && slide.summary !== "Loading summary...") {
        displaySummaryWithStreaming(slide.summary, slideSummary);
        return;
    }
    
    // Show loading state
    slideSummary.innerHTML = `
        <div class="text-center p-4">
            <div class="spinner-border text-primary" role="status">
                <span class="visually-hidden">Loading...</span>
            </div>
            <p class="mt-3">Generating summary...</p>
        </div>
    `;
    
    // Stream the summary
    streamSummary(slide.slideNumber);
}

// Function to stream summary for a specific slide
function streamSummary(slideNumber, forceRegenerate = false) {
    if (!sessionId) {
        console.error("No session ID available");
        return;
    }
    
    // Track loading state
    isSummaryLoading = true;
    
    // Show loading state
    slideSummary.innerHTML = `
        <div class="text-center p-4">
            <div class="spinner-border text-primary" role="status">
                <span class="visually-hidden">Loading...</span>
            </div>
            <p class="mt-3">Generating summary for slide ${slideNumber}...</p>
        </div>
    `;
    
    // Update UI to show generating state
    const regenerateBtn = document.getElementById('regenerate-summary');
    if (regenerateBtn) {
        regenerateBtn.disabled = true;
        regenerateBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Generating...';
    }
    
    // Reset the summary text
    let summaryText = "";
    let isStreamComplete = false;
    let streamTimeout = null;
    
    // Create a new EventSource for server-sent events
    console.log(`Creating EventSource for slide ${slideNumber}, force_regenerate=${forceRegenerate}`);
    const eventSource = new EventSource(
        `/stream_summary?session_id=${sessionId}&slide_num=${slideNumber}` + 
        (forceRegenerate ? '&force_regenerate=true' : '')
    );
    
    // Set a timeout to close the connection if it gets stuck
    streamTimeout = setTimeout(() => {
        console.warn(`Stream timeout after 30 seconds for slide ${slideNumber}`);
        if (!isStreamComplete) {
            eventSource.close();
            
            // Display the partial summary if we have something
            if (summaryText && summaryText.length > 30) {
                console.log(`Using partial summary for slide ${slideNumber}, length: ${summaryText.length} chars`);
                
                // Add an indicator that this was cut off
                if (!summaryText.endsWith('...')) {
                    summaryText += '...';
                }
                
                // Show the partial summary
                slideSummary.innerHTML = renderMarkdown(summaryText);
                
                // Update the slide data with the partial summary
                const slideIndex = slideData.findIndex(s => s.slideNumber === slideNumber);
                if (slideIndex !== -1) {
                    slideData[slideIndex].summary = summaryText;
                    slideData[slideIndex].isLoading = false;
                }
                
                // Show a note that this was cut off
                slideSummary.innerHTML += `
                    <div class="alert alert-info mt-3">
                        <i class="fas fa-info-circle me-2"></i>
                        Note: The summary was cut off. You can click "Regenerate" to try again.
                    </div>
                `;
            } else {
                // No usable content, show error
                slideSummary.innerHTML = `
                    <div class="alert alert-warning">
                        <i class="fas fa-exclamation-triangle me-2"></i>
                        The summary generation timed out. Please try again.
                    </div>
                `;
            }
            
            // Reset button states
            if (regenerateBtn) {
                regenerateBtn.disabled = false;
                regenerateBtn.innerHTML = '<i class="fas fa-redo-alt"></i> Regenerate';
            }
            
            // Reset loading flag
            isSummaryLoading = false;
        }
    }, 30000); // 30 second timeout
    
    // Add an event for connection open
    eventSource.addEventListener('open', function(e) {
        console.log(`EventSource connection opened for slide ${slideNumber}`);
    });
    
    // Handle title events
    eventSource.addEventListener('title', function(e) {
        console.log(`Received title for slide ${slideNumber}: ${e.data}`);
        slideTitle.textContent = e.data;
        
        // Update the slide data
        const slideIndex = slideData.findIndex(s => s.slideNumber === slideNumber);
        if (slideIndex !== -1) {
            slideData[slideIndex].title = e.data;
        }
    });
    
    // Handle progress events
    eventSource.addEventListener('progress', function(e) {
        console.log(`Progress update for slide ${slideNumber}: ${e.data}`);
        // Show progress message
        slideSummary.innerHTML = `
            <div class="text-center">
                <div class="spinner-border text-primary" role="status">
                    <span class="visually-hidden">Loading...</span>
                </div>
                <p class="mt-2">${e.data}</p>
            </div>
        `;
    });
    
    // Handle content chunks
    eventSource.addEventListener('chunk', function(e) {
        // Reset the timeout on each chunk
        clearTimeout(streamTimeout);
        streamTimeout = setTimeout(() => {
            console.warn(`Stream timeout after inactivity for slide ${slideNumber}`);
            if (!isStreamComplete) {
                eventSource.close();
                
                // If we have a substantial summary, use it
                if (summaryText && summaryText.length > 50) {
                    console.log(`Using partial summary after timeout for slide ${slideNumber}`);
                    
                    // Update the slide data with the partial summary
                    const slideIndex = slideData.findIndex(s => s.slideNumber === slideNumber);
                    if (slideIndex !== -1) {
                        slideData[slideIndex].summary = summaryText;
                        slideData[slideIndex].isLoading = false;
                    }
                    
                    // Add a note about being cut off
                    slideSummary.innerHTML = renderMarkdown(summaryText);
                    slideSummary.innerHTML += `
                        <div class="alert alert-info mt-3">
                            <i class="fas fa-info-circle me-2"></i>
                            Note: The summary was cut off. You can click "Regenerate" to try again.
                        </div>
                    `;
                }
                
                // Reset button states
                if (regenerateBtn) {
                    regenerateBtn.disabled = false;
                    regenerateBtn.innerHTML = '<i class="fas fa-redo-alt"></i> Regenerate';
                }
                
                // Reset loading flag
                isSummaryLoading = false;
            }
        }, 10000);
        
        if (summaryText === "") {
            // Clear the loading indicator on first chunk
            slideSummary.innerHTML = "";
        }
        
        // Append the new chunk
        summaryText += e.data;
        
        // Update the display
        slideSummary.innerHTML = renderMarkdown(summaryText);
    });
    
    // Handle complete summary
    eventSource.addEventListener('summary', function(e) {
        console.log(`Received complete summary for slide ${slideNumber}`);
        // This is used for non-streaming summaries (fallback)
        summaryText = e.data;
        slideSummary.innerHTML = renderMarkdown(summaryText);
        
        // Update the slide data
        const slideIndex = slideData.findIndex(s => s.slideNumber === slideNumber);
        if (slideIndex !== -1) {
            slideData[slideIndex].summary = summaryText;
            slideData[slideIndex].isLoading = false;
        }
    });
    
    // Handle completion
    eventSource.addEventListener('done', function(e) {
        console.log(`Summary streaming complete for slide ${slideNumber}`);
        isStreamComplete = true;
        
        // Clear any pending timeouts
        clearTimeout(streamTimeout);
        
        // Close the connection
        eventSource.close();
        
        // Update the slide data
        if (summaryText) {
            const slideIndex = slideData.findIndex(s => s.slideNumber === slideNumber);
            if (slideIndex !== -1) {
                slideData[slideIndex].summary = summaryText;
                slideData[slideIndex].isLoading = false;
            }
        }
        
        // Reset loading state for regenerate button if it exists
        if (regenerateBtn) {
            regenerateBtn.disabled = false;
            regenerateBtn.innerHTML = '<i class="fas fa-redo-alt"></i> Regenerate';
        }
        
        // Reset loading flag
        isSummaryLoading = false;
    });
    
    // Handle errors
    eventSource.addEventListener('error', function(e) {
        console.error("Error streaming summary:", e);
        
        // Mark as complete to prevent timeout handlers
        isStreamComplete = true;
        
        // Clear any pending timeouts
        clearTimeout(streamTimeout);
        
        // Close the connection
        eventSource.close();
        
        // If we have partial content that's substantial, use it
        if (summaryText && summaryText.length > 50) {
            console.log(`Using partial summary after error for slide ${slideNumber}`);
            slideSummary.innerHTML = renderMarkdown(summaryText);
            
            // Update the slide data
            const slideIndex = slideData.findIndex(s => s.slideNumber === slideNumber);
            if (slideIndex !== -1) {
                slideData[slideIndex].summary = summaryText;
                slideData[slideIndex].isLoading = false;
            }
            
            // Add a note about the error
            slideSummary.innerHTML += `
                <div class="alert alert-warning mt-3">
                    <i class="fas fa-exclamation-triangle me-2"></i>
                    Note: An error occurred during summary generation. This partial summary may be incomplete.
                </div>
            `;
        } else {
            // Show error in the summary panel
            slideSummary.innerHTML = `
                <div class="alert alert-warning">
                    <i class="fas fa-exclamation-triangle me-2"></i>
                    Error generating summary. Please try again later.
                </div>
                <p>Basic information about Slide ${slideNumber} will be shown instead.</p>
            `;
            
            // Update with basic information
            const slideIndex = slideData.findIndex(s => s.slideNumber === slideNumber);
            if (slideIndex !== -1) {
                const basicSummary = `**Slide ${slideNumber}** - Basic information only. A detailed summary could not be generated.`;
                slideData[slideIndex].summary = basicSummary;
                slideData[slideIndex].isLoading = false;
                
                // Show the basic summary
                setTimeout(() => {
                    slideSummary.innerHTML = renderMarkdown(basicSummary);
                }, 2000);
            }
        }
        
        // Reset loading state for regenerate button if it exists
        if (regenerateBtn) {
            regenerateBtn.disabled = false;
            regenerateBtn.innerHTML = '<i class="fas fa-redo-alt"></i> Regenerate';
        }
        
        // Reset loading flag
        isSummaryLoading = false;
    });
}

// Handle sending chat messages
async function sendMessage() {
    const message = userInput.value.trim();
    
    if (!message) return;
    
    // Clear input
    userInput.value = '';
    
    // Add user message to chat
    addMessageToChat(message, 'user');
    
    // Add loading indicator
    const loadingId = addLoadingIndicator();
    
    try {
        const response = await fetch('/chat', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                message: message,
                session_id: sessionId,
                current_slide: slideData[currentSlideIndex]?.slideNumber // Add current slide info
            })
        });
        
        const data = await response.json();
        
        // Remove loading indicator
        removeLoadingIndicator(loadingId);
        
        if (response.ok) {
            // Add bot response to chat with streaming effect
            addMessageToChat(data.response, 'bot', true);
        } else {
            // Add error message
            addMessageToChat(`Error: ${data.error}`, 'error');
        }
    } catch (error) {
        console.error('Error sending message:', error);
        
        // Remove loading indicator
        removeLoadingIndicator(loadingId);
        
        // Add error message
        addMessageToChat('Failed to send message. Please try again.', 'error');
    }
    
    // Scroll to bottom of chat
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

// Function to add a message to the chat
function addMessageToChat(message, sender, useStreaming = false) {
    const messageElement = document.createElement('div');
    messageElement.className = sender + '-message';
    
    const messageBubble = document.createElement('div');
    messageBubble.className = 'message-bubble';
    
    // Format the message (apply markdown-like formatting)
    let formattedMessage = message;
    
    if (sender === 'user') {
        // For user messages, just escape HTML to prevent injection
        formattedMessage = escapeHTML(message);
        messageBubble.innerHTML = `<p class="mb-0">${formattedMessage}</p>`;
    } else if (sender === 'bot') {
        // For bot messages, process markdown
        if (useStreaming) {
            // Add an empty paragraph that will be populated with streaming text
            messageBubble.innerHTML = `<p class="mb-0"></p>`;
            messageElement.appendChild(messageBubble);
            chatMessages.appendChild(messageElement);
            
            // Now create the streaming effect
            const textElement = messageBubble.querySelector('p');
            simulateStreamingText(message, textElement);
            return; // Return early as we'll populate the text with streaming
        } else {
            // Regular display without streaming
            formattedMessage = processMarkdown(message);
            messageBubble.innerHTML = `<p class="mb-0">${formattedMessage}</p>`;
        }
    } else {
        // For error messages
        formattedMessage = escapeHTML(message);
        messageBubble.innerHTML = `<p class="mb-0 text-danger">${formattedMessage}</p>`;
    }
    
    messageElement.appendChild(messageBubble);
    chatMessages.appendChild(messageElement);
}

// Function to simulate text streaming for chat responses
function simulateStreamingText(fullText, element) {
    let displayText = '';
    let charIndex = 0;
    const speed = 20; // milliseconds per character
    
    function typeNextChar() {
        if (charIndex < fullText.length) {
            // Add the next character
            displayText += fullText.charAt(charIndex);
            // Format with markdown and display
            element.innerHTML = processMarkdown(displayText) + '<span class="cursor-blink">|</span>';
            // Scroll to bottom as new text appears
            chatMessages.scrollTop = chatMessages.scrollHeight;
            charIndex++;
            // Schedule the next character
            setTimeout(typeNextChar, speed);
        } else {
            // Finished typing, remove cursor
            element.innerHTML = processMarkdown(fullText);
        }
    }
    
    // Start the typing animation
    typeNextChar();
}

// Helper function to process markdown-like formatting
function processMarkdown(text) {
    // Replace newlines with <br> tags
    let processed = text.replace(/\n/g, '<br>');
    
    // Convert **text** to <strong>text</strong> (bold)
    processed = processed.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
    
    // Convert *text* to <em>text</em> (italic)
    processed = processed.replace(/\*(.*?)\*/g, '<em>$1</em>');
    
    // Convert `code` to <code>code</code>
    processed = processed.replace(/`([^`]+)`/g, '<code>$1</code>');
    
    return processed;
}

// Helper function to escape HTML
function escapeHTML(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Function to add a loading indicator
function addLoadingIndicator() {
    const loadingId = 'loading-' + Date.now();
    const loadingElement = document.createElement('div');
    loadingElement.className = 'bot-message';
    loadingElement.id = loadingId;
    
    loadingElement.innerHTML = `
        <div class="message-bubble loading">
            <div class="typing-indicator">
                <span></span>
                <span></span>
                <span></span>
            </div>
        </div>
    `;
    
    chatMessages.appendChild(loadingElement);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    
    return loadingId;
}

// Function to remove a loading indicator
function removeLoadingIndicator(loadingId) {
    const loadingElement = document.getElementById(loadingId);
    if (loadingElement) {
        chatMessages.removeChild(loadingElement);
    }
}

// Add CSS for cursor blinking animation
function addStreamingStyles() {
    const style = document.createElement('style');
    style.textContent = `
        @keyframes blink {
            0%, 100% { opacity: 1; }
            50% { opacity: 0; }
        }
        .cursor-blink {
            animation: blink 1s infinite;
            display: inline-block;
            width: 2px;
            height: 14px;
            background-color: currentColor;
            vertical-align: middle;
        }
    `;
    document.head.appendChild(style);
}

// Event listeners for chat
sendButton.addEventListener('click', sendMessage);

userInput.addEventListener('keypress', function(e) {
    if (e.key === 'Enter') {
        sendMessage();
    }
});

// Initialize streaming styles 
addStreamingStyles();

// Function to process markdown in text
function renderMarkdown(text) {
    if (!text) return '';
    
    // Process LaTeX formulas
    // Inline formulas ($ $)
    text = text.replace(/\$([^\$]+)\$/g, '<span class="latex-formula">$1</span>');
    
    // Block formulas ($$ $$)
    text = text.replace(/\$\$([^\$]+)\$\$/g, '<div class="latex-formula-block">$1</div>');
    
    // Process bold text
    text = text.replace(/\*\*([^\*]+)\*\*/g, '<strong>$1</strong>');
    
    // Process italic text
    text = text.replace(/\*([^\*]+)\*/g, '<em>$1</em>');
    
    // Process code blocks
    text = text.replace(/```([^`]*)```/g, '<pre><code>$1</code></pre>');
    
    // Process inline code
    text = text.replace(/`([^`]*)`/g, '<code>$1</code>');
    
    // Replace newlines with <br>
    text = text.replace(/\n/g, '<br>');
    
    return text;
}

// Find the displayChatResponse function and modify it to use renderMarkdown
function displayChatResponse(response, isStreaming = false) {
    const chatOutput = document.getElementById('chat-output');
    
    if (isStreaming) {
        // For streaming, update the last message
        let lastMessage = chatOutput.lastElementChild;
        if (lastMessage && lastMessage.classList.contains('bot-message')) {
            // Replace the content with rendered markdown
            lastMessage.querySelector('.message-content').innerHTML = renderMarkdown(response);
        } else {
            // Create a new message
            const messageElement = document.createElement('div');
            messageElement.className = 'message bot-message';
            messageElement.innerHTML = `
                <div class="message-avatar">
                    <i class="fas fa-robot"></i>
                </div>
                <div class="message-content">${renderMarkdown(response)}</div>
            `;
            chatOutput.appendChild(messageElement);
        }
    } else {
        // For non-streaming, create a new message
        const messageElement = document.createElement('div');
        messageElement.className = 'message bot-message';
        messageElement.innerHTML = `
            <div class="message-avatar">
                <i class="fas fa-robot"></i>
            </div>
            <div class="message-content">${renderMarkdown(response)}</div>
        `;
        chatOutput.appendChild(messageElement);
    }
    
    // Scroll to the bottom
    chatOutput.scrollTop = chatOutput.scrollHeight;
    
    // Remove loading state
    document.getElementById('loading-indicator').style.display = 'none';
    document.getElementById('user-input').disabled = false;
    document.getElementById('chat-form').classList.remove('loading');
}

// Find the displaySummary function and modify it to use renderMarkdown
function displaySummary(summary, slideTitle, slideNumber) {
    const summaryContainer = document.getElementById('slide-summary');
    if (!summaryContainer) return;
    
    // Update title and slide number
    document.getElementById('slide-title').textContent = slideTitle || `Slide ${slideNumber}`;
    
    // Update summary content with markdown rendering
    summaryContainer.innerHTML = renderMarkdown(summary);
    
    // Show the summary section
    document.getElementById('summary-section').style.display = 'block';
}

// Handle displaySummaryWithStreaming to render markdown properly
function displaySummaryWithStreaming(summary, elementOrTitle, slideNumberOrNothing) {
    // Check if this is the old function signature (with elementToUpdate) or the new one (with title and slide number)
    if (typeof elementOrTitle === 'string' || typeof slideNumberOrNothing === 'number') {
        // This is the new function signature - title and slide number
        const summaryContainer = document.getElementById('slide-summary');
        if (!summaryContainer) return;
        
        // Display the title immediately if provided
        if (typeof elementOrTitle === 'string') {
            document.getElementById('slide-title').textContent = elementOrTitle;
        }
        
        // Clear any existing content
        summaryContainer.innerHTML = '';
        
        // Render the formatted content directly
        summaryContainer.innerHTML = renderMarkdown(summary);
    } else {
        // This is the old function signature with elementToUpdate
        const elementToUpdate = elementOrTitle;
        
        // Clear any previous timers
        const timerId = elementToUpdate.dataset.timerId;
        if (timerId) {
            clearInterval(parseInt(timerId));
        }
        
        // Simply update the element with formatted content
        elementToUpdate.innerHTML = renderMarkdown(summary);
    }
}

// Add the regenerate button event listener after document loads
document.addEventListener('DOMContentLoaded', function() {
    // Add regenerate button functionality
    const regenerateBtn = document.getElementById('regenerate-summary');
    if (regenerateBtn) {
        regenerateBtn.addEventListener('click', function() {
            if (!sessionId || currentSlideIndex === undefined || isSummaryLoading) {
                return; // Don't do anything if we don't have session, slide or are already loading
            }
            
            // Get current slide number
            const slideNumber = slideData[currentSlideIndex]?.slideNumber;
            if (!slideNumber) return;
            
            // Show loading state
            slideSummary.innerHTML = `
                <div class="text-center p-4">
                    <div class="spinner-border text-primary" role="status">
                        <span class="visually-hidden">Loading...</span>
                    </div>
                    <p class="mt-3">Regenerating summary...</p>
                </div>
            `;
            
            // Set loading flag
            isSummaryLoading = true;
            
            // Disable regenerate button temporarily
            regenerateBtn.disabled = true;
            regenerateBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Regenerating...';
            
            // Stream a new summary (force regeneration)
            streamSummary(slideNumber, true);
        });
    }
    
    // Rest of existing DOMContentLoaded code...
});
