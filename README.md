# tv2arr

tv2arr watches the latest movies section of [TV3 Play](https://play.tv3.lv/filmas/), the [Apollo Kino catalogue](https://www.apollokino.lv/movies), and Replay's [Mākslas filmas collection](https://replay.lsm.lv/lv/skaties/raidijums/252/makslas-filmas), matches listings through Seerr, and requests movies originally made in Latvian or English.

## Run on your LAN

1. Copy `.env.example` to `.env`. Set the Docker host's private IPv4 address, the permitted LAN subnet in CIDR notation, and an administrator password of at least 12 characters. A random password can be generated with `openssl rand -hex 24`. Keep the file private (`chmod 600 .env`).
2. On the Docker host, run `docker compose up -d --build`.
3. From a permitted LAN device, open `http://<host-lan-ip>:8080` and sign in.
4. In **Settings**, enter your Seerr base URL and its API key from **Settings → General** in Seerr. Choose **Test connection and save**. The URL must be reachable from the tv2arr container. If Seerr is in another Compose project, use its published LAN URL or connect both services to a shared Docker network and use its service name. Check that Seerr has a working Radarr configuration.
5. The first scan starts after settings are saved. You can also use **Scan now** on the overview. Open **Movies** or **Review queue** to verify matches and request eligible films.

Compose publishes the UI on the configured host IPv4 address and the app checks the connection's source IP against the configured subnet. This setup expects Docker Engine on Linux with LAN client addresses preserved. Use a Docker-aware ingress firewall to permit that subnet and block guest, WAN, and other networks. Keep router port forwarding and public tunnels disabled for this port. Verify both a permitted LAN connection and rejection from outside the subnet. The default Compose UI uses HTTP on the trusted LAN.

## Scans and requests

- Scans run every six hours by default. Movie lookup runs as part of a scan and starts shortly after you connect or successfully retest Seerr. Movies from the first scan are eligible for automatic requests too.
- Scan activity tables show the source and Listed, New and Requested counts in separate columns. Requested counts only new submissions sent during that source's scan; Notes preserves setup messages and errors. Older saved scan summaries are split into the same columns when displayed.
- Apollo Kino uses its public JSON feed at `https://www.apollokino.lv/xml/Events/`, fetching the catalogue and metadata once per scan. It includes current and upcoming releases; ballet, concert and opera genres and events not marked as movies are skipped. Seerr still verifies the original language, independently of dubbed screening languages. Failed, empty or malformed feeds preserve the last successful Apollo listings and log an error; the other sources continue scanning.
- REplay.lv reads every page of collection 252 through the public catalogue API used by Replay's website, with its Latvian/video request settings. Only full videos from that collection are included. Known genre suffixes are removed from Latvian titles for matching; original titles and production years come from each film's metadata, never its broadcast date. Missing or ambiguous metadata stays unverified. Empty, incomplete, duplicate, changing or failed catalogue pages preserve the previous listings and log an error. Individual metadata failures retry on later scans. Requests still require Seerr to verify the original language as Latvian or English.
- A unique title-and-year match can become eligible automatically. Ambiguous matches go to the review queue, where you can search Seerr or enter a TMDB movie ID. Seerr must confirm the original language is Latvian or English before a request can be sent.
- Searches with no movie candidates show **Missing from TMDb** in the review queue and a dedicated movie filter. tv2arr retries them during scans once 24 hours have passed, while they remain on their source. This means Seerr found no candidates, not that absence from TMDb has been confirmed: try alternative titles or match by TMDb ID. If the movie is genuinely missing, contribute a TMDb entry with its IMDb ID. Search errors are logged separately and do not mark movies as missing. Existing unmatched listings are searched again after upgrading.
- Movie titles use the matched movie's original title from Seerr when available, then the source's original title, then its Latvian listing title. The Latvian title remains secondary text on the detail page. Existing matches also use original titles from their saved search results.
- **Ignore movie** on a movie profile hides that listing from every view except **Ignored**, excludes it from dashboard listing counts, and skips its lookups and requests. The choice survives rescans, rematching and restarts. Ignored listings remain accessible even after leaving their source. **Restore movie** keeps the saved match and resumes normal handling; in Automatic mode, restoring a present listing starts a scan. Ignoring does not cancel an existing Seerr request or delete request history.
- Manual mode is the default. Preview mode disables sending. Automatic mode requests all eligible, unrequested matches still on the source, including existing movies and manually saved matches. Enabling Automatic mode or saving an eligible match in that mode starts a scan. The per-scan cap counts new requests sent across all sources; remaining movies are handled on later scans. Movies already requested or available in Seerr are recorded without submitting another request.
- Local history tracks one request per TMDB movie across sources and qualities. tv2arr checks Seerr before submitting a request, even after the local record expires. If a submission has an uncertain outcome, use **Check Seerr again** on the movie to reconcile it.
- Nightly maintenance at 04:00 Europe/Riga deletes movie request history past its first anniversary and catches up when the container restarts. **History → Movie history** shows paginated requests in a compact table. **History → Nightly maintenance** shows the scan and maintenance log, including deletion counts, errors and the latest cleanup even when it falls outside the recent log entries.

## Storage and updates

The named volume `tv2arr-data` holds the SQLite database. It includes the Seerr API key, so protect volume backups. For a file-level backup, stop tv2arr before copying the contents of the volume and start the service again afterward. Run `docker compose up -d --build` after uploading an updated release. Keep the data volume to preserve settings and history.

Matched movies use their TMDB poster supplied through Seerr, falling back to the source image when no poster is available. Scans fetch posters for existing matches that have not had one checked yet. The interface bundles the Font Awesome Free 7.2.0 solid font and its license locally.

## Development

Install `requirements.txt` in a Python 3.14 virtual environment. Run `python -m unittest discover -s tests` for the integration tests.
