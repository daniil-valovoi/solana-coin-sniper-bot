import asyncio
import os
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
import qrcode
from dotenv import load_dotenv

load_dotenv()

api_id = int(os.environ.get("TG_API_ID"))
api_hash = os.environ.get("TG_API_HASH")

async def main():
    # Targets the exact session name your sniper uses
    client = TelegramClient('session_sniper', api_id, api_hash)
    await client.connect()

    if await client.is_user_authorized():
        print("[SUCCESS] Already logged in! Run your sniper script.")
        await client.disconnect()
        return

    print("[KEYGEN] Requesting safe QR Login matrix...")
    qr_login = await client.qr_login()
    
    # Generate the terminal block layout using the standard qrcode engine
    qr = qrcode.QRCode()
    qr.add_data(qr_login.url)  # Use Telethon's verified native login URL
    qr.make(fit=True)
    
    print("\n")
    # Invert=True formats the matrix correctly for dark-themed terminal windows
    qr.print_ascii(invert=True) 
    print("\n⚠️ SCAN THIS QR CODE NOW WITH YOUR TELEGRAM APP ⚠️")
    print("Go to: Telegram Mobile App -> Settings -> Devices -> Link Desktop Device\n")

    try:
        # Wait up to 60 seconds for you to scan it on your phone
        await qr_login.wait(timeout=60)
        print("\n[SUCCESS] QR Code matched! Session file generated.")
    except SessionPasswordNeededError:
        print("\n[2FA REQUIRED] Your account has a Cloud Password enabled.")
        # Fallback query to securely grab your cloud password input
        password = input("Please type your Telegram Cloud Two-Step Password: ")
        await client.sign_in(password=password)
        print("\n[SUCCESS] Cloud Password accepted! Session file generated.")
    except asyncio.TimeoutError:
        print("\n[ERROR] Scanning timed out. Run the script to try again.")
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
