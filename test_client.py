import asyncio
from onvif_client import ONVIFPTZClient

async def run_test():
    ip = '172.18.212.18'
    port = 80
    user = 'admin'
    pwd = 'Supervisor'
    
    client = ONVIFPTZClient(ip, port, user, pwd)
    try:
        print("Connecting...")
        await client.connect()
        print("Connected.")
        
        # Test get_stream_uri
        try:
            uri = await client.get_stream_uri()
            print(f"Stream URI: {uri}")
        except Exception as e:
            print(f"get_stream_uri failed: {e}")
            
        # Test get_status
        try:
            status = await client.get_status()
            print(f"Status: {status}")
        except Exception as e:
            print(f"get_status failed: {e}")
            
        # Test get_presets
        try:
            presets = await client.get_presets()
            print(f"Presets: {presets}")
        except Exception as e:
            print(f"get_presets failed: {e}")

        # Test get_video_encoder_settings
        try:
            video_settings = await client.get_video_encoder_settings()
            print(f"Video Settings: {video_settings}")
        except Exception as e:
            print(f"get_video_encoder_settings failed: {e}")

        # Test get_imaging_settings
        try:
            imaging_settings = await client.get_imaging_settings()
            print(f"Imaging Settings: {imaging_settings}")
        except Exception as e:
            print(f"get_imaging_settings failed: {e}")

    except Exception as e:
        print(f"Connection/Client error: {e}")
    finally:
        await client.disconnect()
        print("Disconnected.")

if __name__ == '__main__':
    asyncio.run(run_test())
