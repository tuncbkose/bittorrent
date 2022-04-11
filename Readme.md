## Dependencies
- aiohttp

## Roadmap
- Bencoding: Done
- .torrent file creation: Done
  - Only supports individual files right now
-  Asynchronous tracker server: Done?
- Client communicating with tracker: mostly done
- Peer-to-peer communications: In-progress
  - plan is to have only peers with entire file to be available for downloads first
  - maybe extend to downloading from peers with partial files later

## Description of what is happening
* [`bencoding.py`](./bencoding.py) contains encoding/decoding functions, a function to create torrent files, and a helper to work with url-encoding
* [`client.py`](./client.py) contains the main logic for a BitTorrent client and a `Manager` class that controls the connections to/from other peers and centralizes file operations
* [`connection.py`](./connection.py) contains a `Connection` class that communicates with peers
* [`tracker.py`](./tracker.py) contains a tracker server

## Assumptions (that may be removed/generalized later)
* Torrent files only contain a single file
* Clients start uploading only after they fully download the file
* Pieces are downloaded without further dividing into blocks
* Possibly other stuff that I am forgetting now

### `client.py` and `connection.py`

```bash
usage: client.py [-h] [-d DOWNLOADED] [--ip IP] [-p PORT] torrent_file
```
**Client**

Calling `client.py` creates a BitTorrent client making connections from given ip and port (by default 127.0.0.8:42420).
If a file is given with the flag `-d`, it will directly listen for connections that will request the file.
Otherwise, it will first download the file, then start listening for connections.

I tried to write it as an asynchronous program. As this was my first time doing so, I am not sure how successful I have been.

The client first registers itself with the tracker server. Then, if the file needs to be downloaded, it starts the manager and occasionally asks the server for more peers (if the manager would like them, up to twice the number of allowed connections).
After the file is downloaded, or if the file was given initially, the client lets the server know that it is done, and starts listening to connections until the program is interrupted.
Received connections are handed to the manager to deal with.

**Manager**

When downloading the file, the manager creates a queue of peers and spawns `MAX_PEER_CONNECTIONS` connections that collect from that queue.
At the moment, my implementation only supports peers uploading after they are done with downloading, meaning that uploading peers have the entire file.
Thus having a queue is not strictly necessary, unless uploading peers end their connections, but it might become more important if I ever allow simultaneous uploads/downloads.
Whenever the connections receive a chunk of the file, the manager is handed the chunk and it saves it on a temporary file.
After all chunks are downloaded, the temporary files are merged into the final file.

When uploading, the client hands the connections it receives to the manager.
If the number of connections doesn't exceed `MAX_PEER_CONNECTIONS`, the manager will spawn a `Connection` to communicate with the peer.
Otherwise, the request will be ignored (I can send them a choke message, put them in the queue and ignore maybe?).

**Connection**

WRITE ME!

### `tracker.py`

Just a webserver via `aiohttp`.