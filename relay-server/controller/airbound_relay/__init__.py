"""AirBound hosted relay: the controller that decides who may use the shared relay.

The relay itself is frps. It routes HTTPS connections by the name in the TLS
handshake (SNI) to the right Home Assistant, without decrypting anything. This
controller is frps's server plugin: it lets a client in only with a valid device
key whose Patreon membership is active, and only for that device's own hostname.
"""
