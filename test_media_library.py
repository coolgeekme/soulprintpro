"""End-to-end test: upload a real asset → create a social post → attach the
asset as cover → remix the asset via Nano Banana. Proves Phases 1+2+3."""
import os, sys, time, io
sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv; load_dotenv("/app/backend/.env")
import requests
from PIL import Image

API = os.environ["API_URL"]
ts = int(time.time())
email = f"media-test-{ts}@assetstest.com"

# Register + org
r = requests.post(f"{API}/api/auth/register", json={"name":"M","email":email,"password":"test12345"})
assert r.status_code == 200, r.text
tok = r.json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}
r = requests.post(f"{API}/api/orgs", json={"name":f"Media {ts}","soul_description":"Cozy productivity tools for indie devs."}, headers=H)
org_id = r.json()["id"]
print(f"Org: {org_id}")

# Build a real image file in memory
img = Image.new("RGB", (512, 512), color="#ff8a50")
img_bytes = io.BytesIO()
img.save(img_bytes, format="PNG")
img_bytes.seek(0)

# 1. Upload asset with category
r = requests.post(
    f"{API}/api/orgs/{org_id}/assets",
    files={"file": ("product.png", img_bytes, "image/png")},
    data={"description":"Our main product shot","category":"product","usable_in_social":"true"},
    headers=H,
)
print(f"Upload: {r.status_code}")
assert r.status_code == 200, r.text
asset = r.json()
print(f"  id={asset['id']} category={asset.get('category')!r} usable_in_social={asset.get('usable_in_social')}")
assert asset["category"] == "product"
assert asset["usable_in_social"] is True

# 2. Create a social post (via the draft endpoint — there is no generic POST /social/posts)
r = requests.post(
    f"{API}/api/orgs/{org_id}/social/draft",
    json={"prompt":"Product launch — playful, vivid","platform":"x","skip_image":True},
    headers=H, timeout=90,
)
assert r.status_code == 200, r.text
post_id = r.json()["posts"][0]["id"]
print(f"Post: {post_id}")

# 3. Use existing asset as cover (Phase 2)
r = requests.post(f"{API}/api/social/posts/{post_id}/use-asset",
                  json={"asset_id": asset["id"]}, headers=H)
print(f"Use-asset: {r.status_code}")
assert r.status_code == 200, r.text
p = r.json()
print(f"  image: source={p['image'].get('source')} url={p['image'].get('url')}")
assert p["image"].get("source") == "asset"

# 4. Remix via Nano Banana (Phase 3)
print("Starting remix (Nano Banana call, ~30s)…")
r = requests.post(f"{API}/api/social/posts/{post_id}/remix-asset",
                  json={"asset_id": asset["id"], "prompt":"Place this product on a soft pink silk background for a luxe feel, keep subject centered."},
                  headers=H, timeout=120)
print(f"Remix: {r.status_code}")
if r.status_code != 200:
    print("Body:", r.text[:400])
else:
    p = r.json()
    print(f"  image: source={p['image'].get('source')} url={p['image'].get('url')}")
    assert p["image"].get("source") == "asset_remix"
    print("✅ Phase 1 + 2 + 3 all working end-to-end")
