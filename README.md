# LLC Scanner

A desktop tool for batch-scanning and identifying Pokémon cards, then generating eBay bulk-upload CSV files. Built with Python and Tkinter.

![Python](https://img.shields.io/badge/Python-3.11+-blue) ![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)

---

## Features

- **Automatic card identification** — drag in a folder of scans and the app identifies every card using perceptual hashing (phash/ahash/dhash/whash) and a DINOv2 embedding matcher against a local TCGdex database of ~22,000 cards
- **Front + Back scan support** — pair front/back scans automatically; both images upload to eBay listings
- **Batch editing** — edit title, set name, condition, finish (holo/non-holo/reverse), edition, quantity, and price per card before export
- **eBay CSV export** — generates a file ready for eBay File Exchange bulk upload, including all item specifics
- **imgbb auto-upload** — scan images are automatically uploaded to imgbb and the URLs are injected into the CSV PicURL field
- **Hover previews** — hover over any scan or reference thumbnail to display a pop-up preview
- **TCG Pocket exclusion** — Pokémon TCG Pocket sets are excluded from matching results by default

---

## Requirements

- Python 3.11+
- Windows (Tkinter GUI)
- GPU recommended (NVIDIA RTX series) for faster DINOv2 embedding computation

---

## Installation

1. **Clone the repo**
   ```
   git clone https://github.com/kfdez/llc-scanner.git
   cd llc-scanner
   ```

2. **Install dependencies**
   ```
   pip install -r requirements.txt
   ```

3. **Install PyTorch with CUDA** (recommended for RTX GPUs)
   ```
   pip install torch --index-url https://download.pytorch.org/whl/cu121
   ```

4. **Run the app**
   ```
   python main.py
   ```

---

## First-Time Setup

On first launch, the app will prompt you to run the setup wizard:

1. **Choose a data directory** — where the card database and images will be stored
2. **Download card data** — fetches metadata for all cards from TCGdex (~22,000 cards)
3. **Download card images** — downloads reference images for matching
4. **Compute hashes** — builds the perceptual hash index
5. **Compute embeddings** — builds the DINOv2 embedding index (requires PyTorch)

Setup only needs to run once. You can re-run individual steps from the **Setup** menu at any time.

---

## Usage

### Batch Scanning

1. Open the **Batch** tab
2. Check **Front + Back scans** if your scans are paired (front1, back1, front2, back2, ...)
3. Click **Open Files** or **Open Folder** to load your scan images
4. Identification starts automatically — results appear as each card is matched
5. Review and adjust each row (title, condition, finish, price, etc.)
6. Click **Export CSV** to generate the eBay upload file

### eBay Export Settings

Configure your eBay settings under **Export → eBay Settings**:

- Site parameters, category ID, store category
- Shipping, return, and payment business policies
- imgbb API key for automatic image hosting
- Listing description HTML template

### imgbb Image Hosting

To have scan images automatically hosted and linked in your CSV:

1. Create a free account at [imgbb.com](https://imgbb.com)
2. Get your API key from [api.imgbb.com](https://api.imgbb.com)
3. Enter the key in **Export → eBay Settings → imgbb API Key**
4. Enable **Auto-upload scans on export**

Images are uploaded at export time and expire after 24 hours (long enough for eBay to transload them).

---

## Project Structure

```
llc-scanner/
├── main.py                  # Entry point
├── config.py                # All constants and settings
├── requirements.txt
├── cards/
│   ├── downloader.py        # TCGdex bulk fetch (metadata + images)
│   ├── hasher.py            # Perceptual hash computation
│   └── embedding_computer.py# DINOv2 embedding computation
├── db/
│   ├── database.py          # SQLite helpers
│   └── schema.sql           # Table definitions
├── identifier/
│   ├── matcher.py           # Hash-based card matcher
│   ├── embedding_matcher.py # DINOv2 embedding matcher
│   ├── enricher.py          # Lazy variant/set_total fetcher
│   └── preprocess.py        # Image preprocessing (card detection)
├── ebay/
│   ├── exporter.py          # eBay CSV builder
│   └── imgbb_uploader.py    # imgbb API uploader
└── gui/
    └── app.py               # Tkinter UI
```

---

## Configuration

Key settings in `config.py`:

| Setting | Default | Description |
|---|---|---|
| `PHASH_SIZE` | 16 | Hash size (16 = 256-bit, better accuracy) |
| `CONFIDENCE_HIGH` | 15 | Hamming distance threshold for high confidence |
| `EMBEDDING_MODEL` | DINOv2 vit_base | ML model for embedding matching |
| `EXCLUDED_SET_ID_PREFIXES` | `["A", "B", "P-A"]` | TCG Pocket sets excluded from results |
| `TOP_K_MATCHES` | 5 | Number of candidates shown per card |

User preferences (data directory, eBay settings, API keys) are stored in `settings.json` (not committed to git).
