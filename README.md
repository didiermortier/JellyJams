<p align="center">
  <img src="jellyjams.jpeg" alt="JellyJams logo" width="220" />
</p>

# JellyJams

JellyJams makes music playlists for your [Jellyfin](https://jellyfin.org/) server on a schedule: one for each of your main genres, one for each decade, one for each of your most-played artists, and private mixes for each user. It runs as one small Docker container with a web page for settings. There is no cloud service and no AI. The playlists come from your own library.

![What it makes in Jellyfin](example.jpg)

This is a maintained fork of [jonasmore/jellyjams](https://github.com/jonasmore/jellyjams), which is no longer updated. It works with Jellyfin 12 and adds safer handling of your own playlists, fewer and better playlists, shipped cover art and a clearer web page.

## What you get

- **Genre playlists** such as "House Radio" and "Hip Hop Radio". By default the 14 biggest genres, plus 2 "spotlight" genres that rotate every week.
- **Decade playlists** such as "Back to the 1980s".
- **Artist playlists** such as "This is Usher!", for your 20 artists with the most tracks.
- **Personal mixes** for each user (top tracks, discovery, recent favourites, genre mix). These need the [Playback Reporting plugin](https://github.com/jellyfin/jellyfin-plugin-playbackreporting) in Jellyfin.
- **Covers** for every playlist. Genre and decade covers ship with the app. Artist covers are the artist's photo with "THIS IS / Artist" on it.
- A **web page** to see what exists, preview a run, change settings and read the log. It works on a phone.

Playlists you made yourself are never changed or deleted. See [Your own playlists are safe](#your-own-playlists-are-safe).

## Quick start

You need Docker, a Jellyfin API key (Jellyfin dashboard, API Keys) and three folders: where JellyJams keeps its data, Jellyfin's `playlists` folder, and your music.

```bash
git clone https://github.com/didiermortier/JellyJams.git
cd JellyJams
docker build -t jellyjams .

docker run -d --name jellyjams -p 5000:5000 \
  -e JELLYFIN_URL=http://jellyfin:8096 \
  -e JELLYFIN_API_KEY=your-key \
  -e TZ=Europe/Brussels \
  -e PUID=1000 -e PGID=1000 \
  -v /path/to/jellyjams-data:/data \
  -v /path/to/jellyfin/config/data/playlists:/playlists \
  -v /path/to/music:/path/to/music:ro \
  jellyjams
```

Open `http://<your-server>:5000`.

- The music folder must be mounted at the **same path Jellyfin sees it under**, so track paths match.
- `PUID` and `PGID` are the user and group Jellyfin runs as, so both can read and write the playlists folder.
- `TZ` is your timezone. Schedule times use the container's clock.

Or use Compose: copy `.env.example` to `.env`, fill it in, and run `docker compose up -d --build`. It builds the image from this repository. On Unraid, use `docker-compose-unraid.yml` the same way.

Everything else is set on the Settings page and has a default that works. Nothing is generated until the schedule fires or you press **Generate now**. Press **Preview (dry run)** first if you want to see what it would do.

## Defaults

| | |
| --- | --- |
| Schedule | Weekly, Sunday at 04:30 (also: daily, every N hours, or manual only) |
| Genre playlists | 14, plus 2 rotating spotlight genres |
| Artist playlists | 20 |
| Playlists in total | 45 genre, decade and artist playlists together (personal mixes not counted) |
| Tracks per playlist | 5 to 100, picked at random from a bigger pool |
| Similar genres | Grouped, so "Classic Rock" and "Hard Rock" become "Rock Radio". A group that would swallow more than a quarter of the library is split again. Turn off "Group similar genres into one playlist" to give every genre its own playlist |

Genre tags are compared without regard to case, so `Hip Hop` and `hip hop` count as one genre.

## Your own playlists are safe

- JellyJams keeps a list of the playlists it created (`/data/managed_playlists.json`). It only replaces or deletes a playlist that is on that list **and** has a name it generates. A name alone never proves anything.
- A playlist of yours with a generated-looking name, for example "Techno Radio", is left alone. The run skips that name instead of making a duplicate.
- **Update covers** and **Delete generated** only act on playlists from that list.
- Replacing is safe: the new playlist is created first and the old one is removed only when that worked.
- Upgrading from a version that kept no list: set `ADOPT_EXISTING_PLAYLISTS=true` for one run so JellyJams takes over the generated names already on the server (each one is logged), then set it back to `false`.

## Preview and clean up

- **Preview (dry run)** on the dashboard, or `docker exec jellyjams python /app/vibecodeplugin.py --dry-run`. It reads your library and writes nothing. It lists what a run would create, replace, skip and retire.
- **Retiring old playlists.** When you lower a cap or switch a type off, playlists from earlier runs stay until you retire them. The dry run lists them under "Would retire". Remove them with `docker exec jellyjams python /app/vibecodeplugin.py --retire-stale`, or set `RETIRE_STALE_PLAYLISTS=true` to do it on every run. Only playlists from the list above are retired. Personal mixes and hand-made playlists never are.

## Covers

- **Genre and decade:** drawn once and shipped in `app/cover`. On start, new ones are copied into `/data/cover` without overwriting anything. To use your own image, put a file with the playlist's name in `/data/cover`, for example `Jazz Radio.jpg`.
- **Artist:** the photo comes from Deezer's public API (no account or key). The artist's name is drawn on it with a bundled font. Switch the name off on the Settings page to keep the plain photo. If Deezer has nothing, the artist's own `folder.jpg` from your music folder is used.
- **Spotify** is optional and tried after Deezer. Its artist search needs a developer app whose owner has a Premium subscription.

Press **Update covers** on the Playlists page to refresh covers without regenerating.

## The web page

- **Dashboard:** connection, counts, the next run, the last run (how long it took, how many errors), a progress bar while a run is going, and what the next run would change.
- **Playlists:** what JellyJams made, grouped by kind, then your hand-made ones. Search, view tracks, delete one of its playlists.
- **Settings:** every setting above. A value that cannot work is refused with the reason, and nothing is saved.
- **Logs:** filter by level, download.

Secrets (the Spotify secret, a Discord webhook) are never sent back to the browser. They show as `********`.

To protect the page with a password, set `WEBUI_BASIC_AUTH_ENABLED=true`, `WEBUI_BASIC_AUTH_USERNAME` and `WEBUI_BASIC_AUTH_PASSWORD`. For anything outside your home network, put it behind a reverse proxy as well.

## Monitoring

Each run writes its result to `/data/run_status.json`. `GET /api/health` returns it, with status 503 when the last run failed, and the container's `HEALTHCHECK` uses the same file. Any uptime monitor can watch it. A Discord webhook for run summaries is optional (`DISCORD_WEBHOOK_ENABLED`, `DISCORD_WEBHOOK_URL`).

## Development

```bash
pip install -r requirements-dev.txt
pytest tests
```

```
app/
  vibecodeplugin.py    the generator (reads Jellyfin, writes playlists)
  webapp.py            the web page and its API
  genre_cover.py       draws genre and artist covers
  cover/               shipped covers
  templates/ static/   web page
tests/                 pytest suite
tools/make_genre_covers.py   how the shipped covers were drawn
```

[SETTINGS.md](SETTINGS.md) has more detail on individual settings. It was written for the original project, so where it disagrees with this page, this page is right.

## License and credits

GPL-3.0, see [LICENSE](LICENSE). Original project by [jonasmore](https://github.com/jonasmore/jellyjams). Built with [Jellyfin](https://jellyfin.org/), [Bootstrap](https://getbootstrap.com/), [Font Awesome](https://fontawesome.com/), [Inter](https://rsms.me/inter/) and [DejaVu Sans](https://dejavu-fonts.github.io/). Cover backgrounds are [Unsplash](https://unsplash.com/license) photos.

Questions and bugs: [open an issue](https://github.com/didiermortier/JellyJams/issues).
