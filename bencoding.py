# Implementation for bencoding encoding and decoding and .torrent file creation
import hashlib
import socket

TORRENT_PIECE_LENGTH = 524288  # 2^19
# TORRENT_PIECE_LENGTH = 2048  # For testing


class TuncError(Exception):
    pass


def decode_url_encoded_bytes(string):
    # Decoding procedure:
    #  - If next char is '%', output the next two chars as bytes
    #  - Else, output the hex representation of next char
    s = b""
    i = 0
    while i < len(string):
        if string[i] == "%":
            s += bytes.fromhex(string[i+1:i+3])
            i += 3
        else:
            s += bytes.fromhex(hex(ord(string[i]))[2:])  # [2:] slicing the output of hex gets rid of "0x" eg. in "0x34"
            i += 1
    return s


def encode(obj):
    # Should take in bytes, integers, lists and dictionaries, the latter two contains all byte types
    # Returns bytes
    # Might need to be careful that integers are represented as integers, not as strings
    t = type(obj)
    if t == bytes:
        return f"{len(obj)}:".encode() + obj
    elif t == int:
        return f"i{str(obj)}e".encode()
    elif t == list:
        return b"l" + b''.join([encode(it) for it in obj]) + b"e"
    elif t == dict:
        return b"d" + b''.join([encode(key)+encode(val) for key, val in obj.items()]) + b"e"
    else:
        raise TuncError(f"Encountered unimplemented type in encoding: {t}")


def decode_helper(bytestring):
    # returns the decoded object and the length of its encoding
    # Always decodes the next object in the string
    key = bytestring[0:1].decode()  # Need to decode this for .isnumeric method. The slicing allows retaining bytes type

    # Next object is a string
    if key.isnumeric():
        c = bytestring.find(b":")
        length = int(bytestring[:c])
        return bytestring[c+1:c+1+length], length+c+1

    # Next object is an integer
    elif key == f"i":
        e = bytestring.find(b"e")
        return int(bytestring[1:e]), e+1

    # Next object is a list
    elif key == f"l":
        s = 1
        lst = []
        while bytestring[s:s+1] != b"e":
            obj, length = decode_helper(bytestring[s:])
            lst.append(obj)
            s += length
        return lst, s+1

    # Next object is a dictionary
    elif key == f"d":
        s = 1
        d = dict()
        while bytestring[s:s+1] != b"e":
            key, length_key = decode_helper(bytestring[s:])
            val, length_val = decode_helper(bytestring[s+length_key:])
            d[key] = val
            s += length_key + length_val
        return d, s+1
    else:
        raise TuncError(f"Encountered unimplemented key in decoding: {key}. Possible parsing error?")


def decode(string):
    return decode_helper(string)[0]


def create_torrent_file(filename, *, tracker_url=None, port=42421):
    # Assumes single file is given and it is in the current directory
    # trackerurl should be "http://ip:port"
    if not tracker_url:
        tracker_url = f"http://{socket.gethostbyname_ex(socket.gethostname())[-1][0]}:{port}"
    d = dict()
    info = dict()
    info[b"name"] = filename.encode()
    info[b"piece length"] = TORRENT_PIECE_LENGTH
    info[b"pieces"] = b""

    with open(filename, "rb") as f:
        contents = f.read()
    offsets = range(0, len(contents), TORRENT_PIECE_LENGTH)
    for piece in (contents[i:i+TORRENT_PIECE_LENGTH] for i in offsets):
        m = hashlib.sha1()
        m.update(piece)
        info[b"pieces"] += m.digest()

    info[b"length"] = len(contents)
    d[b"info"] = info
    d[b"announce"] = tracker_url.encode()
    with open(f"{filename}.torrent", "wb") as f:
        f.write(encode(d))
    print(f"Created torrent for {filename}.")
