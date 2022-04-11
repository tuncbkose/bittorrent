import asyncio


class Connection:

    def __init__(self, manager, info_hash, client_id, *, queue=None, reader=None, writer=None):
        if queue and (reader | writer):
            raise Exception("Connections should either be initialized with a peer queueu (to download) "
                            "or reader and writer (to upload).")
        if queue:  # outgoing connection
            self.type_ = "outgoing"
        else:
            self.type_ = "incoming"

        self.info_hash_ = info_hash
        self.client_id_ = client_id
        self.manager_ = manager
        self.peer_queue_ = queue
        self.reader_ = reader
        self.writer_ = writer
        self.peer_id_ = None

        # Download related
        self.active_ = False
        self.assignment_ = None
        self.being_choked_ = True
        self.interested_ = False

        # Upload related
        self.choking_ = True
        self.remote_interested_ = False
        self.can_send_ = False

        self.last_interaction_time_ = None

    async def handshake(self):
        self.writer_.write(b"19BitTorrent protocol00000000" + self.info_hash_ + self.client_id_)
        await self.writer_.drain()
        recv_handshake = await self.reader_.readexactly(69)  # read the length of a handshake
        if recv_handshake[:29] == b"19BitTorrent protocol00000000" and recv_handshake[29:49] == self.info_hash_:
            if not self.peer_id_:
                self.peer_id_ = recv_handshake[49:]
                self.active_ = True
            elif self.peer_id_ != recv_handshake[49:]:
                self.active_ = False

    async def send_message(self, message, data=None):
        # for request, should figure it out from self.assignment_
        if message == "keep alive":
            self.writer_.write((0).to_bytes(4, "big"))

        elif message == "choke":
            self.writer_.write((256).to_bytes(5, "big"))  # gives <0001><0>

        elif message == "unchoke":
            self.writer_.write((257).to_bytes(5, "big"))  # gives <0001><1>

        elif message == "interested":
            self.writer_.write((258).to_bytes(5, "big"))

        elif message == "not interested":
            self.writer_.write((259).to_bytes(5, "big"))

        elif message == "have":
            raise Exception("Not Implemented")  # Because uploaders are assumed to have the whole file

        elif message == "bitfield":
            raise Exception("Not Implemented")  # Because uploaders are assumed to have the whole file

        elif message == "request":
            prefix = (13).to_bytes(4, "big") + (6).to_bytes(1, "big")  # length prefix + id
            index = self.assignment_.to_bytes(1, "big")
            begin = (0).to_bytes(1, "big")
            length = self.manager_.piece_length_.to_bytes(1, "big")
            self.writer_.write(prefix+id+index+begin+length)

        elif message == "piece":
            # if piece message is given, data should be a bytestring
            prefix = (9+len(data)).to_bytes(4, "big") + (7).to_bytes(1, "big")  # length prefix + id
            index = self.assignment_.to_bytes(1, "big")
            begin = (0).to_bytes(1, "big")
            self.writer_.write(prefix+index+begin+data)
        elif message == "cancel":  # Right now this doesn't matter because we assume uploaders have entire file
            # TODO Implement
            pass
        await self.writer_.drain()

    async def receive_message(self):
        message = await self.reader_.read()  # Not sure if this is reading the right amount. Might need to send EOF
        length = int.from_bytes(message[:4], "big")
        id = int.from_bytes(message[4:5], "big")
        payload = message[5:]
        op = None

        if length == 0:
            op = "keep alive"
        elif id == 0:
            op = "choke"
        elif id == 1:
            op = "unchoke"
        elif id == 2:
            op = "interested"
        elif id == 3:
            op = "not interested"
        elif id == 4:
            op = "have"
        elif id == 5:
            # There is likely a better way to write this using int.from_bytes, but since I am not using it yet, I won't
            op = "bitfield"
            temp = payload  # bitfield is supposed to be a bitstring, but at least for now I'll keep it bytestring
            payload = [0] * len(temp)
            for i in range(len(temp)):
                payload[i] = int(temp[i:i + 1] == b"1")
        elif id == 6:
            op = "request"
        elif id == 7:
            op = "piece"
        elif id == 8:
            op = "cancel"

        return op, payload

    async def run_to_download(self):
        # TODO rewrite with the assumption that every downloaded piece is piece_length
        """
        Outgoing connection to download.
        First, get an assignment from manager to figure out what data to request
        Then, get a peer from the queue and establish connection/handshake.
        If successful, let them know we are interested and wait until unchoked.
        Then request the data, give it to manager when received
        """

        if self.type_ != "outgoing":
            raise Exception("Only outgoing connections should be used to download.")
        while True:
            if not self.active_:

                if not self.assignment_:  # If we don't have a piece assigned to download, get one
                    self.assignment_ = self.manager_.get_assignment()

                if self.assignment_:  # If we know what to download, but don't yet have an active connection
                    self.active_ = True
                    peer_ip, peer_port = await self.peer_queue_.get()
                    self.reader_, self.writer_ = await asyncio.open_connection(peer_ip, peer_port)
                    shook_hands = await self.handshake()

                    if not shook_hands:  # If we had a problem establishing connection, move onto next peer
                        self.active_ = False
                        self.writer_.close()
                        await self.writer_.wait_closed()
                        continue

                    self.being_choked_ = True
                    self.interested_ = False
                    await self.send_message("interested")

                else:  # The other connections will download all remaining pieces, so we are done here.
                    self.writer_.close()
                    await self.writer_.wait_closed()
                    return

            if not self.being_choked_:
                await self.send_message("request")

            message, payload = await self.receive_message()
            if message == "keep alive":  # Right now this doesn't matter because we assume little waiting time
                pass
            elif message == "choke":
                self.being_choked_ = True
            elif message == "unchoke":
                self.being_choked_ = False
            elif message == "interested":  # Right now this doesn't matter because we are downloading only
                pass
            elif message == "not interested":  # Right now this doesn't matter because we are downloading only
                pass
            elif message == "have":  # Right now this doesn't matter because we assume uploaders have entire file
                pass
            elif message == "bitfield":  # Right now this doesn't matter because we assume uploaders have entire file
                pass
            elif message == "request":  # This shouldn't happen as this connection will only be for download
                "Send error message?"
                pass
            elif message == "piece":
                # Since we are downloading one piece at a time instead of dividing to blocks, this is simpler
                self.manager_.handle_received_block(payload)
                self.active_ = False
                self.assignment_ = None
            elif message == "cancel":  # Right now this doesn't matter because we assume uploaders have entire file
                pass

    async def run_to_upload(self):
        if self.type_ != "incoming":
            raise Exception("Only incoming connections should be used to upload.")

        shook_hands = await self.handshake()
        if not shook_hands:  # If we had a problem establishing connection, give up
            return
        self.choking_ = True
        self.remote_interested_ = False
        while True:
            message, payload = await self.receive_message()

            if not self.remote_interested_ and message == "interested":  # The first message we expect is interested
                self.remote_interested_ = True
                await self.send_message("unchoke")
                self.choking_ = False

            elif message == "keep alive":  # Right now this doesn't matter because we assume little waiting time
                pass
            elif message == "choke":  # Doesn't matter, we are uploading
                pass
            elif message == "unchoke":  # Doesn't matter, we are uploading
                pass
            elif message == "not interested":
                self.remote_interested_ = False
            elif message == "have":  # Doesn't matter, we are uploading
                pass
            elif message == "bitfield":  # Doesn't matter, we are uploading
                pass
            elif message == "request":
                if self.choking_:
                    continue
                can_send, data = self.manager_.check_for_block(payload)
                if can_send:
                    self.can_send_ = True
                    await self.send_message("piece", data)
                    self.can_send_ = False
            elif message == "piece":  # Doesn't matter, we are uploading
                pass
            elif message == "cancel":
                self.can_send_ = False



