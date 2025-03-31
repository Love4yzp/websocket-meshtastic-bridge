```json Modbus RTU v1
{
  "i": "X", // target EDGE_X
  "t": "r", // connection_type: Modebus_RTU
  "p": "0", // port_number 0
  "c": { // command for Modbus RTU
    "a": 1, // slave address
    "f": 3, // function code [1,2,3,4] [5,6,16]
    "r": 40001, // register code
    "n": 2, // quantity
    "d": [1234] // data value
  },
  "ss": 1234     // session(timestamp) (保留，使用 ss 作为简写)
}
```

```json Modbus TCP v1
{
  "i": 2,              // ~5 bytes  ("i":2,)
  "t": "t",           // ~6 bytes  ("t":"t",)
  "h": "127.0.0.1",   // ~17 bytes ("h":"127.0.0.1",)
  "p": 502,           // ~8 bytes  ("p":502,)
  "c": {              // ~4 bytes  ("c":{)
    "f": 3,           // ~5 bytes  ("f":3,)
    "a": 2, //unitId
    "r": 40001,       // ~10 bytes ("r":40001,)
    "n": 2,           // ~6 bytes  ("n":2,)
    "d": [1234,5678]  // ~20 bytes ("d":[1234,5678])
  },                  // ~1 byte   (})
  "ss": 1710633600123 // ~21 bytes ("ss":1710633600123)
}                     // ~1 byte   (})
```

```json response
{
  "payload" :{
    "i": "ROOT_ID", // short name of ROOT
    "tp": "type of device",
    "ss": 1710633600123,
    "d": [,,,,],
  },
 "channel": "1"
}
```
