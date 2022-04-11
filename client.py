# Implementation of a bittorrent client
import argparse
import asyncio
import time
import hashlib
import bencoding
import aiohttp
import urllib.parse
import tempfile
import math
from typing import Union, Optional
from connection import Connection

CLIENT_PORT = 42420
MAX_PEER_CONNECTIONS = 10


class Manager:

    def __init__(self, piece_length, total_length, output_name, info_hash, client_id, file_downloaded=False):
        # If file is already downloaded, it is assumed to be in the current directory.

        # File related
        self.bitfield_ = [0] * math.ceil(total_length / piece_length)
        self.assigned_ = -1
        self.piece_length_ = piece_length
        self.filename_ = output_name
        self.total_length_ = total_length
        self.info_hash_ = info_hash

        # We will keep downloaded pieces in temporary files during download and upload
        self.pieces_ = [tempfile.TemporaryFile() for i in range(len(self.bitfield_))]
        if file_downloaded:
            with open(output_name, "rb") as file:
                text = file.read()
                for i in range(len(self.bitfield_)):
                    self.pieces_[i].write(text[i*self.piece_length_:(i+1)*self.piece_length_])
                    self.pieces_[i].seek(0)  # put the stream back at the start to make reading easier

        # Client related
        self.client_id_ = client_id

        # Download related
        self.downloaded_ = 0
        if not file_downloaded:
            self.peers_queue_ = asyncio.Queue(2*MAX_PEER_CONNECTIONS)  # Collect twice the allowed number of connections
            self.download_connections_ = [Connection(self, self.info_hash_, self.client_id_, queue=self.peers_queue_) for i in range(MAX_PEER_CONNECTIONS)]

        # Upload related
        self.uploaded_ = 0
        self.num_incoming_connections_ = 0

    def combine_temp_files(self):
        with open(self.filename_, "wb") as output:
            for temp in self.pieces_:
                output.write(temp.read())
                temp.seek(0)

    async def run(self):
        await asyncio.gather(*[self.download_connections_[i].run_to_download() for i in range(MAX_PEER_CONNECTIONS)])
        self.combine_temp_files()

    def get_assignment(self):
        # tells which block to download
        self.assigned_ += 1
        if self.assigned_ < len(self.bitfield_):
            return str(self.assigned_)  # string because this will later be converted to bytes
        return None

    async def handle_incoming_connection(self, reader, writer):
        """
        It seems doable to create a second queue and a set of connections so that when we have max connections
        we can just send a "choked" message until we can take up new connections, but for now I'll just reject instead
        """
        if self.num_incoming_connections_ >= MAX_PEER_CONNECTIONS:
            writer.close()
            await writer.wait_closed()
        else:
            c = Connection(self, self.info_hash_, self.client_id_, reader=reader, writer=writer)
            await c.run_to_upload()

    def want_more_peers(self):
        return not self.peers_queue_.full()

    def add_peers(self, list_of_peers):
        # It is possible I will have duplicate peers
        for peer in list_of_peers:
            if not self.peers_queue_.full():
                self.peers_queue_.put_nowait(peer)

    def check_for_block(self, payload):  # we are assuming client only uploads when they have the file so this is easy
        # by assumption, length = piece length
        index = int.from_bytes(payload[0:4], "big")
        begin = int.from_bytes(payload[4:8], "big")
        length = int.from_bytes(payload[8:], "big")
        data = self.pieces_[index].read()
        self.pieces_[index].seek(0)
        return True, data

    def handle_received_block(self, payload):
        # by assumption, length = piece length
        index = int.from_bytes(payload[0:4], "big")
        begin = int.from_bytes(payload[4:8], "big")
        block = payload[8:]
        self.pieces_[index].write(block)
        self.pieces_[index].seek(0)

    def close_files(self):
        for file in self.pieces_:
            file.close()


def extract_response_parameters(response):
    # Converts url-encoded response into dictionary
    # response obtained in Client.send_tracker_request
    d = urllib.parse.parse_qs(response, keep_blank_values=True)
    d["complete"] = int(d["complete"][0])
    d["incomplete"] = int(d["incomplete"][0])
    d["interval"] = int(d["interval"][0])

    compact = d["peers"][0]
    d["peers"] = []
    for i in range(len(compact)//12):  # parse_qs gives everything in string
        peer = compact[i:i+12]
        ip = ".".join([str(int(peer[j:j+2], base=16)) for j in range(0, 8, 2)])
        port = int(peer[8:12], base=16)
        d["peers"].append((ip, port))
    return d


class Client:

    def __init__(self, torrent_d, ip="", port=42420,  already_has_file=False):
        # torrent_d is the dictionary created from reading the torrent file
        self.d_: dict[bytes, Union[bytes, int]] = torrent_d
        self.tracker_addr_: str = self.d_[b"announce"].decode()

        # Address client will use to start and accept connections
        self.ip_: str = ip
        self.port_: int = port

        # Tracker related info
        self.client_id_: str = "42"+str(time.time_ns())
        self.tracker_id_: Optional[str] = None  # ID given by tracker
        self.interval_: Optional[int] = None  # Time to wait between tracker requests

        m = hashlib.sha1()
        bencoded_info = bencoding.encode(self.d_[b"info"])  # This should be 18 bytes long for a long while
        m.update(bencoded_info)
        self.info_hash_: bytes = m.digest()

        # Transfer related info
        self.file_done_downloading_: bool = already_has_file
        self.manager_ = Manager(piece_length=self.d_[b"info"][b"piece length"],
                                total_length=self.d_[b"info"][b"length"],
                                output_name=self.d_[b"info"][b"name"],
                                info_hash=self.info_hash_,
                                client_id=self.client_id_,
                                file_downloaded=already_has_file)

    async def run(self):

        response = await self.send_tracker_request("started")  # Let server register us
        print(response)
        self.interval_ = response["interval"]
        print(self.interval_)

        # If we need to download the file, start the download manager
        if not self.file_done_downloading_:
            """
            create Connection objects with async run methods
            """
            # wait for downloads to finish
            # all connections should automatically stop when download ends
            self.manager_.add_peers(response["peers"])
            await self.manager_.run()

        # Loop during download to fetch more peers
        while not self.file_done_downloading_:
            # Sleep first to avoid contacting tracker immediately after the previous one
            asyncio.sleep(self.interval_)
            if self.manager_.want_more_peers():
                response = await self.send_tracker_request()
                self.manager_.add_peers(response["peers"])

        # Either we already have file or we finished downloading
        # So change into serving other requesters
        await self.send_tracker_request("completed")  # We don't care about what server might tell us at this point
        server = await asyncio.start_server(self.handle_connection, self.ip_, self.port_)

        async with server:
            await server.serve_forever()

    async def handle_connection(self, reader, writer):
        # For handling requests from other peers
        await self.manager_.handle_incoming_connection(reader, writer)

    async def send_tracker_request(self, event=None):
        # uploaded: bytes uploaded so far
        # downloaded: bytes downloaded so far
        # left: bytes left to download
        # event: one of "started", "stopped", "completed"
        # TODO proper handling for 'uploaded', 'downloaded' and 'left'
        payload = {
            'info_hash': self.info_hash_,
            'peer_id': self.client_id_,
            'port': self.port_,
            'uploaded': 0,
            'downloaded': 0,
            'left': 0,
            'compact': 1,
            'event': event
        }
        # Optional fields
        if event:
            payload["event"] = event
        if self.tracker_id_:
            payload["trackerid"] = self.tracker_id_

        params = urllib.parse.urlencode(payload)
        async with aiohttp.ClientSession() as session:
            async with session.get(self.tracker_addr_, params=params) as response:
                # For debug
                a = await response.text()
                print(a)
                return extract_response_parameters(a)

    def send_shutdown_message(self):
        # asyncio.get_event_loop().run_until_complete(self.send_tracker_request(0, 0, 0, "stopped"))
        asyncio.run(self.send_tracker_request("stopped"))

    def shutdown(self):
        self.send_shutdown_message()
        self.manager_.close_files()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("torrent_file", help="path to torrent file")
    parser.add_argument("-d", "--downloaded", help="path to file if already downloaded")
    parser.add_argument("--ip", default="127.0.0.8", help="ip address for client")
    parser.add_argument("-p", "--port", type=int, default=42420, help="port for client")
    args = parser.parse_args()
    with open(args.torrent_file, "rb") as f:
        torrent_d = bencoding.decode(f.read())
    if args.downloaded:
        # TODO check if given torrent file matches the file (look at the hash?)
        client = Client(torrent_d, args.ip, args.port, already_has_file=True)
    else:
        client = Client(torrent_d, args.ip, args.port, already_has_file=False)

    try:
        print("Client starting.")
        asyncio.run(client.run())
    except KeyboardInterrupt:
        client.shutdown()
        print("Interrupt encountered. Closing client.")
