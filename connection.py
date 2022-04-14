import asyncio


ETB = (23).to_bytes(1, "big")  # End of transmission block character used to mark read/write blocks for asyncio
DEBUG_ID = 0  # To differentiate between connections when debugging, each gets a unique one


class Connection:

    def __init__(self, manager, info_hash, client_id, debug=False, *, queue=None, reader=None, writer=None):
        # I thought `None or None` would be False, but it is empty, so this is a workaround
        if queue and not not (reader or writer):
            raise Exception("Connections should either be initialized with a peer queue (to download) "
                            "or reader and writer (to upload).")
        if queue:  # outgoing connection
            self.type_ = "outgoing"
        else:
            self.type_ = "incoming"

        self.info_hash_ = info_hash
        self.client_id_ = client_id.encode()  # I don't think we ever need this as a string
        self.manager_ = manager
        self.peer_queue_ = queue
        self.reader_ = reader
        self.writer_ = writer
        self.peer_id_ = None
        self.debug_ = debug

        # Download related
        self.active_ = False
        self.assignment_ = None
        self.being_choked_ = True
        self.interested_ = False

        # Upload related
        self.choking_ = True
        self.remote_interested_ = False
        self.can_send_ = False

    async def initiate_handshake(self):
        # For downloading connections
        self.writer_.write(b"19BitTorrent protocol00000000" + self.info_hash_ + self.client_id_ + ETB)
        await self.writer_.drain()
        recv_handshake = await self.reader_.readuntil(ETB)  # read the length of a handshake
        recv_handshake = recv_handshake[:-1]  # get rid of extra ETB
        if recv_handshake[:29] == b"19BitTorrent protocol00000000" and recv_handshake[29:49] == self.info_hash_:
            if not self.peer_id_:
                self.peer_id_ = recv_handshake[49:]
                self.active_ = True
                return True
        return False

    async def expect_handshake(self):
        # For uploading connections
        recv_handshake = await self.reader_.readuntil(ETB)  # read the length of a handshake
        recv_handshake = recv_handshake[:-1]  # get rid of extra ETB
        if recv_handshake[:29] == b"19BitTorrent protocol00000000" and recv_handshake[29:49] == self.info_hash_:
            if not self.peer_id_:
                self.peer_id_ = recv_handshake[49:]
                if self.debug_:
                    print(f"peer_id={self.peer_id_}")
                self.active_ = True
                self.writer_.write(b"19BitTorrent protocol00000000" + self.info_hash_ + self.client_id_+ETB)
                await self.writer_.drain()
                return True
        return False

    async def send_message(self, message, data=None):
        # for request, should figure it out from self.assignment_
        if message == "keep alive":
            self.writer_.write((0).to_bytes(4, "big"))  # gives <0000>

        elif message == "choke":
            self.writer_.write((256).to_bytes(5, "big"))  # gives <0001><0>

        elif message == "unchoke":
            self.writer_.write((257).to_bytes(5, "big"))  # gives <0001><1>

        elif message == "interested":
            self.writer_.write((258).to_bytes(5, "big"))  # gives <0001><2>

        elif message == "not interested":
            self.writer_.write((259).to_bytes(5, "big"))  # gives <0001><3>

        elif message == "have":
            raise Exception("Not Implemented")  # Because uploaders are assumed to have the whole file

        elif message == "bitfield":
            raise Exception("Not Implemented")  # Because uploaders are assumed to have the whole file

        elif message == "request":
            prefix = (13).to_bytes(4, "big") + (6).to_bytes(1, "big")  # length prefix + id <0013><6>
            index = self.assignment_.to_bytes(4, "big")  # the index of the piece to download
            begin = (0).to_bytes(4, "big")  # by assumption, we download the entire piece
            length = self.manager_.piece_length_.to_bytes(4, "big")
            self.writer_.write(prefix+index+begin+length)

        elif message == "piece":
            # if piece message is given, data should be a bytestring
            prefix = (9+len(data)).to_bytes(4, "big") + (7).to_bytes(1, "big")  # length prefix + id
            index = self.assignment_.to_bytes(4, "big")  # the index of the piece to download
            begin = (0).to_bytes(4, "big")  # by assumption, we download the entire piece
            self.writer_.write(prefix+index+begin+data)

        elif message == "cancel":  # Right now this doesn't matter because we assume uploaders have entire file
            pass
        self.writer_.write(ETB)  # need to end message with etb so reading will work properly
        await self.writer_.drain()

    async def receive_message(self):
        message = await self.reader_.readuntil(ETB)
        length = int.from_bytes(message[:4], "big")
        id = int.from_bytes(message[4:5], "big")
        payload = message[5:][:-1]  # second slice is to get rid of '\n' added by sender
        op = None
        if self.debug_:
            print(f"message = {message}, length = {length}, id = {id}")

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
        global DEBUG_ID
        """
        Outgoing connection to download.
        First, get an assignment from manager to figure out what data to request
        Then, get a peer from the queue and establish connection/handshake.
        If successful, let them know we are interested and wait until unchoked.
        Then request the data, give it to manager when received
        """
        debug_id = DEBUG_ID
        DEBUG_ID += 1
        if self.debug_:
            print(f"{debug_id}: run_to_download called")

        if self.type_ != "outgoing":
            raise Exception("Only outgoing connections should be used to download.")

        while True:
            if self.debug_:
                print(f"{debug_id}: Start of run_to_download loop")

            if not self.active_:
                if self.debug_:
                    print(f"{debug_id}: not active")

                if not self.assignment_:  # If we don't have a piece assigned to download, get one
                    self.assignment_ = self.manager_.get_assignment()
                    if self.debug_:
                        print(f"{debug_id}: Received assignment {self.assignment_}")

                if self.assignment_ is not None:  # If we know what to download, but don't yet have an active connection
                    if self.debug_:
                        print(f"{debug_id}: Assignment is not None")

                    self.active_ = True
                    peer_ip, peer_port = await self.peer_queue_.get()  # get peer to connect to
                    if self.debug_:
                        print(f"{debug_id}: About to open connection")

                    self.reader_, self.writer_ = await asyncio.open_connection(peer_ip, peer_port)
                    if self.debug_:
                        print(f"{debug_id}: Send connection command")

                    shook_hands = await self.initiate_handshake()

                    if not shook_hands:  # If we had a problem establishing connection, move onto next peer
                        if self.debug_:
                            print(f"{debug_id}: Handshake failed")
                        self.active_ = False
                        self.writer_.close()
                        await self.writer_.wait_closed()
                        continue

                    if self.debug_:
                        print(f"{debug_id}: Shook hands")
                        print(f"{debug_id}: Sending interested message")
                    self.being_choked_ = True
                    self.interested_ = False
                    await self.send_message("interested")
                    if self.debug_:
                        print(f"{debug_id}: Sent interested message")

                else:  # The other connections will download all remaining pieces, so we are done here.
                    if self.debug_:
                        print(f"{debug_id}: Assignment is None, closing")
                    if self.writer_:
                        self.writer_.close()
                        if self.debug_:
                            print(f"{debug_id}: About to close")
                        await self.writer_.wait_closed()
                        if self.debug_:
                            print(f"{debug_id}: Closed")
                        self.peer_queue_.put_nowait((peer_ip, peer_port))
                        # This peer is usable by other connections, but this way also means if a file has more than
                        # client.MAX_PEER_CONNECTIONS pieces, it won't be downloaded in full
                    return

            if not self.being_choked_:
                if self.debug_:
                    print(f"{debug_id}: Unchoked")
                await self.send_message("request")

            if self.debug_:
                print(f"{debug_id}: Waiting for unchoke or other message message")
            try:
                message, payload = await self.receive_message()
            except asyncio.IncompleteReadError:
                message = None

            if message == "keep alive":  # Right now this doesn't matter because we assume little waiting time
                pass
            elif message == "choke":
                if self.debug_:
                    print(f"{debug_id}: received choke")
                self.being_choked_ = True
            elif message == "unchoke":
                if self.debug_:
                    print(f"{debug_id}: received unchoke")
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
                if self.debug_:
                    print(f"{debug_id}: received piece")
                self.manager_.handle_received_block(payload)
                self.active_ = False
                self.assignment_ = None
            elif message == "cancel":  # Right now this doesn't matter because we assume uploaders have entire file
                pass

    async def run_to_upload(self):
        global DEBUG_ID
        debug_id = DEBUG_ID
        DEBUG_ID += 1

        if self.debug_:
            print(f"{debug_id}: run_to_upload called")

        if self.type_ != "incoming":
            raise Exception("Only incoming connections should be used to upload.")

        if self.debug_:
            print(f"{debug_id}: Expecting handshake")
        shook_hands = await self.expect_handshake()
        if not shook_hands:  # If we had a problem establishing connection, give up
            if self.debug_:
                print(f"{debug_id}: Problem in handshake")
            return
        if self.debug_:
            print(f"{debug_id}: Shook hands")
        self.choking_ = True
        self.remote_interested_ = False
        while True:
            try:
                message, payload = await self.receive_message()
                if self.debug_:
                    print(f"{debug_id}: Received message")
                    print(f"{debug_id}: message: {message}")
            except asyncio.IncompleteReadError:
                message = None

            if not self.remote_interested_ and message == "interested":  # The first message we expect is interested
                if self.debug_:
                    print(f"{debug_id}: Setting interested to True")
                self.remote_interested_ = True
                if self.debug_:
                    print(f"{debug_id}: Sending unchoke")
                await self.send_message("unchoke")
                self.choking_ = False

            elif message == "keep alive":  # Right now this doesn't matter because we assume little waiting time
                pass
            elif message == "choke":  # Doesn't matter, we are uploading
                pass
            elif message == "unchoke":  # Doesn't matter, we are uploading
                pass
            elif message == "not interested":
                if self.debug_:
                    print(f"{debug_id}: received not interested")
                self.remote_interested_ = False
            elif message == "have":  # Doesn't matter, we are uploading
                pass
            elif message == "bitfield":  # Doesn't matter, we are uploading
                pass
            elif message == "request":
                if self.debug_:
                    print(f"{debug_id}: received request")
                if self.choking_:
                    if self.debug_:
                        print(f"{debug_id}: request ignored due to choking")
                    continue
                can_send, index, data = self.manager_.check_for_block(payload)
                if can_send:
                    if self.debug_:
                        print(f"{debug_id}: request sending")
                    self.can_send_ = True
                    self.assignment_ = index  # Sending piece message uses it
                    await self.send_message("piece", data)
                    self.assignment_ = None
                    self.can_send_ = False
                    if self.debug_:
                        print(f"{debug_id}: Ending run_to_upload")
                    return
            elif message == "piece":  # Doesn't matter, we are uploading
                pass
            elif message == "cancel":
                self.can_send_ = False



