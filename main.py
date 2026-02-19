"""
Pokemon Card Identifier — entry point

Usage:
    python main.py                                  Launch the desktop GUI
    python main.py --setup                          Download metadata + images + hashes + embeddings
    python main.py --embed                          Compute ML embeddings only (images already downloaded)
    python main.py --identify <path>                Identify a card (default: ML matcher)
    python main.py --identify <path> --matcher hash|ml|hybrid
"""

import sys


def run_gui():
    from gui.app import launch
    launch()


def run_setup():
    from db.database import init_db
    from cards.downloader import download_all
    from cards.hasher import compute_all_hashes
    from cards.embedding_computer import compute_all_embeddings

    def log(msg):
        print(msg, flush=True)

    print("=== Pokemon Card Identifier — Database Setup ===\n")
    init_db()
    download_all(progress_callback=log)
    print("\nComputing perceptual hashes...")
    compute_all_hashes(progress_callback=log)
    print("\nComputing ML embeddings...")
    compute_all_embeddings(progress_callback=log)
    print("\nSetup complete.")


def run_embed():
    """Compute embeddings only — useful after adding new cards without re-downloading."""
    from db.database import init_db
    from cards.embedding_computer import compute_all_embeddings

    def log(msg):
        print(msg, flush=True)

    print("=== Pokemon Card Identifier — Embedding Computation ===\n")
    init_db()
    compute_all_embeddings(progress_callback=log)
    print("\nEmbedding complete.")


def run_identify(image_path: str, matcher: str = "ml"):
    from db.database import init_db
    init_db()

    print(f"Identifying: {image_path}  [matcher={matcher}]\n")

    if matcher == "hash":
        from identifier.matcher import identify_card
        results = identify_card(image_path)
        score_key = "dist"

    elif matcher == "ml":
        from identifier.embedding_matcher import identify_card_embedding
        from identifier.matcher import identify_card
        results = identify_card_embedding(image_path)
        score_key = "sim"
        if not results:
            print("No embeddings found, falling back to hash matcher.")
            results = identify_card(image_path)
            score_key = "dist"

    else:  # hybrid
        from identifier.embedding_matcher import identify_card_embedding
        from identifier.matcher import identify_card
        from config import EMBEDDING_CONFIDENCE_MED

        ml_results   = identify_card_embedding(image_path)
        hash_results = identify_card(image_path)

        if ml_results and ml_results[0]["distance"] >= EMBEDDING_CONFIDENCE_MED:
            results = ml_results
            score_key = "sim"
        else:
            results = hash_results if hash_results else ml_results
            score_key = "dist" if results is hash_results else "sim"

    if not results:
        print("No matches found.")
        return

    for i, r in enumerate(results):
        marker = ">>>" if i == 0 else "   "
        print(
            f"{marker} [{r['confidence'].upper():6s}] {score_key}={r['distance']:.4f}  "
            f"{r['name']:30s}  {r['set_name']} #{r['number']}  ({r['rarity']})"
        )


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--setup" in args:
        run_setup()

    elif "--embed" in args:
        run_embed()

    elif "--identify" in args:
        idx = args.index("--identify")
        try:
            path = args[idx + 1]
        except IndexError:
            print("Usage: python main.py --identify <image_path> [--matcher hash|ml|hybrid]")
            sys.exit(1)

        matcher = "ml"  # default to ML matcher
        if "--matcher" in args:
            m_idx = args.index("--matcher")
            try:
                matcher = args[m_idx + 1]
                if matcher not in ("hash", "ml", "hybrid"):
                    print("--matcher must be one of: hash, ml, hybrid")
                    sys.exit(1)
            except IndexError:
                print("--matcher requires a value: hash, ml, or hybrid")
                sys.exit(1)

        run_identify(path, matcher)

    else:
        run_gui()
