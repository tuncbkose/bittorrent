# Note
To test it, you might need to regenerate the torrent file by
```python
import bencoding
bencoding.create_torrent_file(filename)
```
I have also observed that on Reed's network sometimes my IP address changed, which requires regenerating the torrent.
If you want to test different sizes for transfered pieces, change `bencoding.TORRENT_PIECE_LENGTH`.

## Running to test
First, generate the torrent file for a desired test file as described above (I ran into an issue on a Reed computer that required hardcoding the ip address but couldn't reproduce it).

Second, start the tracker server with `python tracker.py`

Third, run a number of clients with the file already downloaded option (description on how to use `client.py` is below).

Lastly, run a client without the file already downloaded option in a directory where the file should be downloaded.

## Dependencies
- aiohttp: To easily have asynchronous http

## Roadmap
- Bencoding: Done
- .torrent file creation: Done
  - Only supports individual files right now
- Asynchronous tracker server: Done
- Client communicating with tracker: Done
- Peer-to-peer communications: Done? 
  - Only peers that have the entire download can upload right now

**Potential improvements**
- .torrent with multiple files
  - This didn't seem very important to the networking aspect of the assignment, so I didn't do it.
- Downloading from peers with partial files
  - I wasn't sure what would be the best way to do this, especially asynchronously. With the current structure I have, I guess I can prompt running `Connection`s to prompt the client to check for whether someone is trying to connect.
  - After figuring out how to connect, the rest should not be too difficult because the manager abstracts away the handling of the file from specific connections.
- Sophisticated queuing and piece downloading
  - I thought implementing the protocol itself would be sufficient for now.
- Make tracker keep track of statistics
  - In the absence of the above, this didn't seem necessary
- Download blocks of pieces instead of entire pieces
  - This just made handling the file much simpler. I don't think it would be too complicated to add.


## Description of what is happening
* [`bencoding.py`](./bencoding.py) contains encoding/decoding functions, a function to create torrent files, and a helper to work with url-encoding
* [`client.py`](./client.py) contains the main logic for a BitTorrent client and a `Manager` class that controls the connections to/from other peers and centralizes file operations
* [`connection.py`](./connection.py) contains a `Connection` class that communicates with peers
* [`tracker.py`](./tracker.py) contains a tracker server

## Assumptions (that may be removed/generalized later) and Known Problems
* Torrent files only contain a single file
* Clients start uploading only after they fully download the file
* Pieces are downloaded without further dividing into blocks
* If `client.MAX_PEER_CONNECTIONS` is smaller than the number of pieces to be downloaded, the download won't be complete.

### `client.py` and `connection.py`

```bash
usage: client.py [-h] [-f FILE] [--ip IP] [-p PORT] [-d] torrent_file

positional arguments:
  torrent_file          path to torrent file

optional arguments:
  -h, --help            show this help message and exit
  -f FILE, --file FILE  path to file if already downloaded
  --ip IP               ip address for client (by default inferred from socket.gethostbyname_ex())
  -p PORT, --port PORT  port for client
  -d, --debug           print debug message
```
**Client**

Calling `client.py` creates a BitTorrent client making connections from a detected ip and given port (by default 42420).
If a file is given with the flag `-f`, it will directly listen for connections that will request the file.
Otherwise, it will first download the file, then start listening for connections.

I tried to write it as an asynchronous program. As this was my first time doing so, I am not entirely sure how successful I have been.

The client first registers itself with the tracker server. Then, if the file needs to be downloaded, it starts the manager and occasionally asks the server for more peers (if the manager would like them, up to twice the number of allowed connections).
After the file is downloaded, or if the file was given initially, the client lets the server know that it is done, and starts listening to connections until the program is interrupted.
Received connections are handed to the manager to deal with.

**Manager**

When downloading the file, the manager creates a queue of peers and spawns `MAX_PEER_CONNECTIONS` connections that collect from that queue.
At the moment, my implementation only supports peers uploading after they are done with downloading, meaning that uploading peers have the entire file.
Thus having a queue is not strictly necessary, unless uploading peers end their connections, but it might become more important if I ever allow simultaneous uploads/downloads.
Whenever the connections receive a chunk of the file, the manager is handed the chunk and it saves it on a temporary file (using the `tempfile` module).
After all chunks are downloaded, the temporary files are merged into the final file.

When uploading, the client hands the connections it receives to the manager.
If the number of connections doesn't exceed `MAX_PEER_CONNECTIONS`, the manager will spawn a `Connection` to communicate with the peer.
Otherwise, the request will be ignored (I can send them a choke message, put them in the queue and ignore maybe?).

**Connection**

*To download:* Fetches an assignment from the manager that tells which piece to download.
Then gets a peer to connect from the queue, connects and sends a handshake request.
If no problems are encountered, sends an "interested" message, waits for an "unchoke" and enters the main loop of sending requests and receiving pieces.

*To upload:* Waits for a handshake and enters a messaging loop. If the peer expresses interest (since by assumption the file is available), sends an "unchoke" message.
Then waits for requests and sends pieces.

### `tracker.py`

Just a webserver via `aiohttp` that reads requests and responds appropriately.
