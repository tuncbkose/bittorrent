# Implementation of a bittorrent tracker server
import argparse
import urllib.parse
import bencoding
import random
import socket
from aiohttp import web


all_peers = set()
completed_peers = set()  # Those who completed the download
peer_2_trackerid = dict()
next_trackerid = 0  # this should be a string when sent


# Converts aiohttp-structured data into dictionary
def extract_request_parameters(param_dict):
    d = dict()
    for key, val in param_dict.items():
        if key == "info_hash":
            val = bencoding.decode_url_encoded_bytes(val)
        d[key] = val
    return d


def sample_peers():
    sample = random.sample(tuple(completed_peers), min(len(completed_peers), 50))  # sampling from set is deprecated
    compact = ""
    print(f"Giving peers: {sample}")
    # put peers in compact form
    for ip, port in sample:
        nums = ip.split(".") + [port]
        for num in nums:
            n = hex(int(num))[2:]
            compact += "0"*(2-len(n))+n  # len(n) can be one, but we need it to be 2
    return compact


async def request_handler(request):
    global next_trackerid

    # Extract the request parameters
    params_urlencoded = request.query
    params = extract_request_parameters(params_urlencoded)
    peer = (request.remote, params["port"])  # ip addr and port pair
    print(f"Received request from {peer}")

    # Form response
    payload = {"complete": len(completed_peers),
               "incomplete": len(all_peers)-len(completed_peers),
               "peers": sample_peers(),
               "interval": 30  # arbitrarily chosen, not sure what would be appropriate
    }

    # Wrong trackerid
    if "trackerid" in params and peer_2_trackerid[peer] != params["trackerid"]:
        return web.Response(text="trackerid of existing peer doesn't match with what is recorded.")

    # Special request?
    if "event" in params:
        event = params["event"]

        if event == "started":
            peer_2_trackerid[peer] = str(next_trackerid)
            next_trackerid += 1
            all_peers.add(peer)

        elif event == "stopped":
            all_peers.discard(peer)
            completed_peers.discard(peer)

        elif event == "completed":
            completed_peers.add(peer)

    params = urllib.parse.urlencode(payload)
    return web.Response(text=params)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", default=None, help="ip address for client")
    parser.add_argument("-p", "--port", type=int, default=42421, help="port for client")
    args = parser.parse_args()

    app = web.Application()
    app.add_routes([web.get('/', request_handler)])
    web.run_app(app, host=args.ip, port=args.port)
