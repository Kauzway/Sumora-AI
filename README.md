# Slide Extraction Chatbot

A web-based chatbot that extracts text from slides (PDF, PPT, PPTX) and allows you to ask questions about the content using the Gemini AI model.

## Features

- Upload slide files (PDF, PPT, PPTX)
- Automatic text extraction from slides
- Chat interface to ask questions about the slide content
- AI-powered responses using Google's Gemini API

## Prerequisites

- Python 3.8 or higher
- Google Gemini API key

## Installation

1. Clone this repository or download the code.

2. Install the required Python packages:
   ```
   pip install -r requirements.txt
   ```

3. Set your Gemini API key as an environment variable:
   ```
   # On macOS/Linux
   export GEMINI_API_KEY="your-api-key"
   
   # On Windows
   set GEMINI_API_KEY=your-api-key
   ```
   
   Alternatively, you can edit the `app.py` file and replace the default API key with your own.

## Project Structure

```
project/
│
├── app.py                  # Main Flask application
├── requirements.txt        # Python dependencies
│
├── static/                 # Static files
│   ├── css/
│   │   └── style.css       # CSS styles for the web interface
│   │
│   └── js/
│       └── script.js       # JavaScript for the web interface
│
├── templates/              # HTML templates
│   └── index.html          # Main page template
│
└── slides/                 # Directory where uploaded slides are stored
```

## How to Use

1. Start the Flask server:
   ```
   python app.py
   ```

2. Open a web browser and go to:
   ```
   http://localhost:5001
   ```

3. Upload your slides (PDF, PPT, or PPTX format).

4. Once processing is complete, use the chat interface to ask questions about the slide content.

5. Your slides will be stored in the `slides` folder. You can add slides manually to this folder as well.

## Supported File Formats

- PDF (.pdf)
- PowerPoint (.ppt, .pptx)

## Troubleshooting

- If you encounter issues with PDF extraction, ensure you have the proper dependencies for PyMuPDF.
- For PowerPoint files, ensure python-pptx is properly installed.
- Check that your Gemini API key is valid and has not reached usage limits.

## License

This project is open source and available under the MIT License. 