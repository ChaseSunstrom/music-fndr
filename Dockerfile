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
ENV PORT=8800 UPDATE_HOURS=24
EXPOSE 8800
# a yt-dlp older than a few weeks fails YouTube downloads: update it on every start
# (and every UPDATE_HOURS while running; UPDATE_HOURS=0 turns both off). Offline,
# this gives up within a minute and starts with the yt-dlp it has.
CMD ["sh", "-c", "[ \"${UPDATE_HOURS:-24}\" = 0 ] || timeout 60 pip install -q -U --no-cache-dir --disable-pip-version-check --timeout 15 --retries 2 'yt-dlp[default]' || echo 'yt-dlp update skipped'; exec uvicorn --app-dir /app app:app --host 0.0.0.0 --port $PORT"]
