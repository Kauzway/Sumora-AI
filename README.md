# Sumora AI

Sumora AI is an intelligent presentation analysis tool that leverages AI to automatically generate concise summaries of presentation slides. It helps users quickly understand and extract key points from PDF presentations without having to read through every slide in detail.

![Sumora AI](static/images/Sumora%20Full%20logo.png)

## Features

- **PDF Presentation Processing**: Upload PDF presentations for AI-powered analysis
- **Slide Summarization**: Automatic generation of concise summaries for each slide
- **OCR Capabilities**: Extracts text from image-based slides using optical character recognition
- **Interactive UI**: Modern, responsive interface with light/dark mode
- **AI Chat Assistance**: Ask questions about any slide or the entire presentation
- **RAG Technology**: Uses Retrieval-Augmented Generation for accurate responses
- **Fast LLM Integration**: Connects to Groq API using Llama 3.1 8B Instant model

## Demo

[Check out the live demo](#) *(Add your deployed link here when available)*

## Getting Started

### Prerequisites

- Python 3.8+
- Groq API key ([Get one here](https://console.groq.com/))
- Tesseract OCR (for image text extraction)

### Installation

1. Clone the repository
   ```
   git clone https://github.com/Kauzway/Sumora.git
   cd Sumora
   ```

2. Create and activate virtual environment
   ```
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install Tesseract OCR
   - **Windows**: Download and install from [GitHub](https://github.com/UB-Mannheim/tesseract/wiki)
   - **Linux**: `sudo apt-get install tesseract-ocr`
   - **macOS**: `brew install tesseract`

4. Install dependencies
   ```
   pip install -r requirements.txt
   ```

5. Create environment variables
   ```
   cp .env.example .env
   ```
   Edit the `.env` file and add your Groq API key

6. Run the application
   ```
   python app.py
   ```

7. Open your browser and go to http://localhost:5002

## Deployment

### Deploying to Google Cloud Run

1. Install Google Cloud SDK
2. Configure Docker for Google Container Registry
   ```
   gcloud auth configure-docker
   ```
3. Build and tag the Docker image
   ```
   docker build -t gcr.io/your-project-id/sumora:latest .
   ```
4. Push the image to Container Registry
   ```
   docker push gcr.io/your-project-id/sumora:latest
   ```
5. Deploy to Cloud Run
   ```
   gcloud run deploy sumora --image gcr.io/your-project-id/sumora:latest --platform managed --allow-unauthenticated --set-env-vars "GROQ_API_KEY=your_groq_api_key"
   ```

### Deploying to Heroku

1. Create a Heroku account
2. Install Heroku CLI
3. Login to Heroku
   ```
   heroku login
   ```
4. Create a new Heroku app
   ```
   heroku create your-app-name
   ```
5. Set environment variables
   ```
   heroku config:set GROQ_API_KEY=your_groq_api_key
   ```
6. Deploy to Heroku
   ```
   git push heroku main
   ```

### Deploying to Other Platforms

This app can be deployed to any platform that supports Python applications. 
Key requirements:
- Set environment variables (especially GROQ_API_KEY)
- Ensure `gunicorn` is installed (included in requirements.txt)
- Ensure Tesseract OCR is installed in the environment
- Point to `app.py` as the main application file

## How to Use

1. **Upload a Presentation**: Click on the upload button and select a PDF file
2. **View Slides**: Navigate through slides using the thumbnails or navigation buttons
3. **Read Summaries**: Each slide will have an AI-generated summary displayed, including text extracted from images
4. **Ask Questions**: Use the chat function to ask questions about any slide or the entire presentation

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgments

- Built with [Flask](https://flask.palletsprojects.com/)
- AI powered by [Groq](https://groq.com/)
- OCR capabilities by [Tesseract](https://github.com/tesseract-ocr/tesseract)
- Vector embeddings by [Sentence Transformers](https://www.sbert.net/)

---

Created by [Kauzway.ai](https://kauzway.ai) 