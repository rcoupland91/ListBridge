# ListBridge

A self-hosted web app that keeps your music playlists in sync across **Plex**, **Navidrome**, and **M3U files**.

- Add a song to a Plex playlist → it appears in Navidrome within 60 seconds
- Delete a song from Navidrome → it's removed from Plex automatically
- Edit an M3U file → changes push to Plex and Navidrome
- Scan your music library to import existing M3U playlists

---

## Requirements

- Docker & Docker Compose
- Plex Media Server with a music library
- Navidrome (optional)
- Your music library accessible to the container

---

## Setup

**1. Clone the repo**
```bash
git clone https://github.com/rcoupland91/ListBridge.git
cd ListBridge
```

**2. Create a `.env` file**
```env
SECRET_KEY=change-me-to-something-random

PLEX_URL=http://<your-plex-ip>:32400
PLEX_TOKEN=<your-plex-token>
PLEX_MUSIC_LIBRARY=Music

NAVIDROME_URL=http://<your-navidrome-ip>:4533
NAVIDROME_USER=admin
NAVIDROME_PASSWORD=<your-navidrome-password>

# Path Plex uses internally for your music (check Plex server settings)
PLEX_PATH_PREFIX=/data/Music

# Path your music is mounted at inside the container
LOCAL_PATH_PREFIX=/music
```

> **Finding your Plex token:** In Plex Web, open any media item, click the `...` menu → Get Info → View XML. The token is the `X-Plex-Token` value in the URL.

**3. Update the volume path in `docker-compose.yml`**

Edit the music volume to point to your library:
```yaml
- /path/to/your/music:/music
```

**4. Start the app**
```bash
docker compose up -d
```

Open **http://localhost:5000** (or your server IP).

---

## Path Prefix Configuration

ListBridge needs to translate between the path Plex stores internally and the path visible inside the container.

| Setting | Example | Description |
|---|---|---|
| `PLEX_PATH_PREFIX` | `/data/Music` | How Plex's Docker container sees your music |
| `LOCAL_PATH_PREFIX` | `/music` | How ListBridge's container sees your music |

If both containers mount the same share at the same path, these can be the same value.

---

## Usage

1. Go to **Settings** and verify Plex and Navidrome show as connected
2. Go to **Playlists → Scan for M3Us** to import existing playlists from your music library
3. Or manually add a playlist and link it to an existing Plex/Navidrome playlist
4. Click **Sync** on any playlist to run an immediate sync
5. From then on, changes in Plex are picked up automatically every 60 seconds

---

## Releasing

Tag a commit to trigger a GitHub Actions release that builds the Docker image and publishes it to GHCR:

```bash
git tag v1.0.0
git push origin v1.0.0
```

The release will be available at `ghcr.io/rcoupland91/listbridge:<version>`.
