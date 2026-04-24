# Sumora-AI

## Environment Variables for Google Cloud Run

When deploying to Google Cloud Run, the following environment variables need to be set:

| Variable | Description | Required |
|----------|-------------|----------|
| `GOOGLE_CLIENT_ID` | OAuth Client ID from Google Cloud Console | Yes |
| `GOOGLE_CLIENT_SECRET` | OAuth Client Secret from Google Cloud Console | Yes |
| `SECRET_KEY` | Secret key for Flask sessions (should be a random string) | Yes |
| `APP_BASE_URL` | Base URL of your application (e.g., https://your-app-name.run.app) | Yes |
| `PRODUCTION` | Set to "true" to enable production settings | Yes |
| `NVIDIA_API_KEY` | API key for NVIDIA NIM (https://build.nvidia.com/) used for AI inference and slide vision transcription | Yes |

### Supported upload formats

PDF and PPTX (and .ppt). PPTX files are converted to PDF via LibreOffice
headless inside the container, then slides are rendered to images and
transcribed by the NVIDIA NIM vision model.

### Steps to obtain Google OAuth credentials:

1. Go to the [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Navigate to "APIs & Services" > "Credentials"
4. Click "Create Credentials" > "OAuth client ID"
5. Select "Web application" as the application type
6. Add your authorized JavaScript origins (your Cloud Run URL)
7. Add your authorized redirect URIs (your Cloud Run URL + "/auth/google/callback")
8. Click "Create" and note your Client ID and Client Secret

### Session Security:
The application will automatically enable secure session cookies when deployed to Cloud Run. This ensures that:
- Cookies are only sent over HTTPS
- Cookies are protected from JavaScript access
- CSRF protection is enforced
