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
import socket
from typing import Union, Optional
from connection import Connection

MAX_PEER_CONNECTIONS = 10


class Manager:

    def __init__(self, piece_length, total_length, output_name,
                 info_hash, client_id, file_downloaded=False, debug=False):
        # If file is already downloaded, it is assumed to be in the current directory.

        # File related
        self.bitfield_ = [0] * math.ceil(total_length / piece_length)  # To keep track of which pieces are downloaded
        self.assigned_ = -1  # Index of the last piece we have given to a connection to download
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
        self.debug_ = debug

        # Download related
        self.downloaded_ = 0
        # The two below are needed for downloading connections, but to make asyncio work (event loops are weird)
        # They need to be initialized a bit later
        if not file_downloaded:
            self.peers_queue_ = None
            self.download_connections_ = None

        # Upload related
        self.uploaded_ = 0
        self.num_incoming_connections_ = 0

    def set_queue(self, queue):
        # asyncio requires the Queue to be created in the function that is called in asyncio.run().
        # This function transfers that queue to the manager
        self.peers_queue_ = queue
        self.download_connections_ = [Connection(self, self.info_hash_, self.client_id_,
                                                 queue=self.peers_queue_, debug=self.debug_)
                                      for i in range(MAX_PEER_CONNECTIONS)]

    def combine_temp_files(self):
        with open(self.filename_, "wb") as output:
            if self.debug_:
                print(f"Combining {len(self.pieces_)} pieces")
            for temp in self.pieces_:
                output.write(temp.read())
                temp.seek(0)

    async def run(self):
        # Download the pieces and combine them
        await asyncio.gather(*[self.download_connections_[i].run_to_download() for i in range(MAX_PEER_CONNECTIONS)])
        if self.debug_:
            print("Manager, combining files")
        self.combine_temp_files()
        if self.debug_:
            print("Done combining")
        print("File downloaded")

    def get_assignment(self):
        # tells which block to download (called from Connections)
        self.assigned_ += 1
        if self.assigned_ < len(self.bitfield_):
            return self.assigned_  # string because this will later be converted to bytes
        return None

    async def handle_incoming_connection(self, reader, writer):
        """
        It seems doable to create a second queue and a set of connections so that when we have max connections
        we can just send a "choked" message until we can take up new connections, but for now I'll just reject instead
        """
        if self.debug_:
            print("Handling incoming connection")
        if self.num_incoming_connections_ >= MAX_PEER_CONNECTIONS:
            if self.debug_:
                print("Connection refused")
            writer.close()
            await writer.wait_closed()
        else:
            if self.debug_:
                print("Connection accepted")
            c = Connection(self, self.info_hash_, self.client_id_, reader=reader, writer=writer, debug=self.debug_)
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
        self.uploaded_ += length
        return True, index, data

    def handle_received_block(self, payload):
        # by assumption, length = piece length
        index = int.from_bytes(payload[0:4], "big")
        begin = int.from_bytes(payload[4:8], "big")  # in current assumptions, this will always be 0
        block = payload[8:]
        self.pieces_[index].write(block)
        self.pieces_[index].seek(0)
        self.downloaded_ += len(block)

    def close_files(self):
        # closes the tempfiles used for download
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
        peer = compact[i*12:(i+1)*12]
        ip = ".".join([str(int(peer[j:j+2], base=16)) for j in range(0, 8, 2)])
        port = int(peer[8:12], base=16)
        d["peers"].append((ip, port))
    return d


class Client:

    def __init__(self, torrent_d, ip="", port=42420,  already_has_file=False, debug=False):
        self.debug_ = debug
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
        self.peer_queue_ = None  # to be handed to self.manager_
        self.manager_ = Manager(piece_length=self.d_[b"info"][b"piece length"],
                                total_length=self.d_[b"info"][b"length"],
                                output_name=self.d_[b"info"][b"name"],
                                info_hash=self.info_hash_,
                                client_id=self.client_id_,
                                file_downloaded=already_has_file,
                                debug=debug)

    async def run(self):
        self.peer_queue_ = asyncio.Queue(2*MAX_PEER_CONNECTIONS)  # Needs to be created in the function in asyncio.run()
        self.manager_.set_queue(self.peer_queue_)

        response = await self.send_tracker_request("started")  # Let server register us
        if self.debug_:
            print(f"tracker response: {response}")
        self.interval_ = response["interval"]

        # If we need to download the file, start the download manager
        if not self.file_done_downloading_:
            # all connections should automatically stop when download ends
            self.manager_.add_peers(response["peers"])
            if self.debug_:
                print("Client running the manager")
            await self.manager_.run()
            self.file_done_downloading_ = True

        # Loop during download to fetch more peers
        # TODO I suspect this part might not be working as intended, it just stops at the await above
        # Potential solution is to prompt client to request more peers from the manager when needed
        while not self.file_done_downloading_:
            # Sleep first to avoid contacting tracker immediately after the previous one
            await asyncio.sleep(self.interval_)
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
        if self.debug_:
            print("received connection")
        await self.manager_.handle_incoming_connection(reader, writer)
        if self.debug_:
            print("handled connection")

    async def send_tracker_request(self, event=None):
        # uploaded: bytes uploaded so far
        # downloaded: bytes downloaded so far
        # left: bytes left to download
        # event: one of "started", "stopped", "completed"
        payload = {
            'info_hash': self.info_hash_,
            'peer_id': self.client_id_,
            'port': self.port_,
            'uploaded': self.manager_.uploaded_,
            'downloaded': self.manager_.downloaded_,
            'left': self.manager_.total_length_ - self.manager_.downloaded_,
            'compact': 1,
            'event': event
        }
        # Optional fields
        if event:  # one of started, completed, stopped
            payload["event"] = event
        if self.tracker_id_:
            payload["trackerid"] = self.tracker_id_

        params = urllib.parse.urlencode(payload)
        async with aiohttp.ClientSession() as session:
            async with session.get(self.tracker_addr_, params=params) as response:
                a = await response.text()
                return extract_response_parameters(a)

    def send_shutdown_message(self):
        asyncio.run(self.send_tracker_request("stopped"))

    def shutdown(self):
        self.send_shutdown_message()
        self.manager_.close_files()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("torrent_file", help="path to torrent file")
    parser.add_argument("-f", "--file", help="path to file if already downloaded")
    parser.add_argument("--ip", default=socket.gethostbyname_ex(socket.gethostname())[-1][0],
                        help="ip address for client (by default inferred from socket.gethostbyname_ex())")
    parser.add_argument("-p", "--port", type=int, default=42420, help="port for client")
    parser.add_argument("-d", "--debug", action="store_true", help="print debug message")
    args = parser.parse_args()
    with open(args.torrent_file, "rb") as f:
        torrent_d = bencoding.decode(f.read())
    if args.downloaded:
        # TODO check if given torrent file matches the file (look at the hash?)
        client = Client(torrent_d, args.ip, args.port, already_has_file=True, debug=args.debug)
    else:
        client = Client(torrent_d, args.ip, args.port, already_has_file=False, debug=args.debug)

    try:
        print("Client starting.")
        asyncio.run(client.run())
    except KeyboardInterrupt:
        client.shutdown()
        print("Interrupt encountered. Closing client.")
        raise  # To go back to proper asyncio exception handling
