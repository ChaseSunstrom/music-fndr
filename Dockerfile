FROM python:3.12-slim

# ffmpeg converts audio; deno lets yt-dlp solve YouTube's JavaScript challenges
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
RUN pip install --no-cache-dir "spotdl~=4.5.2" "spotapi~=1.2.8"

COPY app.py /app/
COPY static /app/static

# downloads are written relative to the working directory, like spotdl
WORKDIR /music
ENV PORT=8800
EXPOSE 8800
CMD ["sh", "-c", "exec uvicorn --app-dir /app app:app --host 0.0.0.0 --port $PORT"]
