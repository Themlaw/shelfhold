"""Reading a Mihon backup (`.tachibk`) as a hostile file (EF-INT-008, ENF-SEC-002, D14).

`decode` is the gzip and protobuf reader written by hand, bounded in size, depth, entries and string
length; `sandbox` runs it in a bounded subprocess; `map` turns what it read into the records
Shelfhold keeps. Nothing here touches the database, the disk or the network.
"""
