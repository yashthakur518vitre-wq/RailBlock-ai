import traceback
import time

print("========================================")
print("STARTING BLOCK PLAN DEBUG TEST")
print("========================================")

try:
    print("\n[1] Importing FastAPI application...")
    from main import app
    print("[OK] main.py imported")

    print("\n[2] Creating TestClient...")
    from fastapi.testclient import TestClient

    client = TestClient(app)
    print("[OK] TestClient created")

    print("\n[3] Sending generate-block-plan request...")
    print("This is the exact point we are testing.")
    print("If it hangs here, the problem is inside the endpoint/optimizer.")
    print()

    start = time.time()

    response = client.post(
        "/generate-block-plan",
        params={
            "horizon": "weekly",
            "regenerate": False,
        },
        json={
            "incompatible_pairs": []
        },
    )

    elapsed = time.time() - start

    print("\n========================================")
    print("REQUEST FINISHED")
    print("========================================")
    print(f"Time taken: {elapsed:.2f} seconds")
    print(f"Status code: {response.status_code}")
    print("Response:")
    print(response.text)

except Exception as e:
    print("\n========================================")
    print("ERROR")
    print("========================================")
    print(type(e).__name__)
    print(str(e))
    print()
    traceback.print_exc()