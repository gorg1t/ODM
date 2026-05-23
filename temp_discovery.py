import sys
from wsdiscovery.discovery import ThreadedWSDiscovery as WSDiscovery

def discover(prefix):
    wsd = WSDiscovery()
    wsd.start()
    ret = wsd.searchServices()
    wsd.stop()
    
    found = []
    for service in ret:
        xaddrs = service.getXAddrs()
        for xaddr in xaddrs:
            if prefix in xaddr:
                # In wsdiscovery 2.x, 'service' itself might have address info or we parse from xaddr
                host = xaddr.split("//")[1].split("/")[0].split(":")[0]
                port = xaddr.split(":")[2].split("/")[0] if len(xaddr.split(":")) > 2 else "80"
                found.append({
                    'host': host,
                    'port': port,
                    'types': [str(t) for t in service.getTypes()],
                    'xaddr': xaddr
                })
    return found

if __name__ == "__main__":
    prefix = "172.18.212"
    devices = discover(prefix)
    for d in devices:
        print(f"Host: {d['host']}")
        print(f"Port: {d['port']}")
        print(f"Types: {d['types']}")
        print(f"XAddr: {d['xaddr']}")
        print("-" * 20)
