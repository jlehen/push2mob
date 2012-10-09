# I. DESCRIPTION

Push2mob is a daemon acting as a common gateway interface for Apple Push
Notification Service (APNS) and Google Cloud Messaging (GCM).

It provides asynchronous notification and feedback services using SQLite
as persistent storage, so it can be stopped at any time without losing
informations.

It is controlled through one ZeroMQ REQ/REP (a.k.a. "ping-pong") socket
for each service with an extremely simple communication protocol.


# II. DESIGN RATIONALE

Commands are asynchronous primarily because this is the ways APNS works.
GCM is synchronous but we can easily mimic an asynchronous behaviour,
while the other way around is not possible.  Push2mob will ensure to
honor GCM feedback while the client application has not retrieved it.

Besides, asynchronous pushes allow the client application to return
immediately without waiting for all notifications to be sent; push2mob
will just do it right (there is not much way to stray anyhow).  This is
especially relevant for APNS where every single device must be addressed
individually.

ZeroMQ protocol has been chosen because it allows very simple network
programming.  It abstracts away all the socket dance one has to do when
writing a networked application.

The ZeroMQ REQ/REP semantic enforces an even simpler programming model:
the client application sends request and then receives a reply.  Any
other sequence (e.g. sending two messages in a row) results in an error.


# III. PROTOCOL

Both APNS and GCM have only two commands, `send` and `feedback`,
though their arguments may differ a little, depending on the
capabilities each one provides.

Protocol is described using the very simple
[Wirth syntax notation](http://en.wikipedia.org/wiki/Wirth_syntax_notation).

## III.1. APNS

For a better grasp of the whole picture, please read the [APNS
documentation](http://developer.apple.com/library/mac/#documentation/NetworkingInternet/Conceptual/RemoteNotificationsPG/ApplePushService/ApplePushService.html#//apple_ref/doc/uid/TP40008194-CH100-SW1)

### III.1.1. `send` command

#### III.1.1.1. Request

    REQUEST = "send" expiry count devicetoken { devicetoken } payload
    devicetoken = dt_hex | dt_base64

* `expiry` is how long the notification is valid.
It may be an absolute value representing is a UNIX epoch date or,
if it is prefixed with a `+` character, the following number is
added to the current UNIX time to compute the expiry date.  An expiry in
the past will make APNS try only once before discarding it.
Note that the notification may be enqueued for some time (usually a few
seconds) in the daemon before being send to APNS, depending on its load.
* `count` is the number of following device tokens.  Device tokens
may be specified in hexadecimal or Base64.
* `dt_hex` is a 32-bytes device token encoded in hexadecimal.
* `dt_base64` is a 32-bytes device token encoded in Base64.
* `payload` is what must be sent to the device.  The format must be
valid JSON and must not exceed 256 bytes (enforced by APNS).  No further
check is done.  This is the only argument that can contain spaces.

#### III.1.1.2. Reply

    RESPONSE = rep_ok | rep_err
    rep_ok = "OK" id { id }
    rep_err = "ERROR" errormsg

* `id` is the notification identifier.  It is unique for each device
token.
* `errormsg` is an error message describing the problem.

If an error happens while trying to send a notification to multiple
devices, nothing will be pushed at all.

#### III.1.1.3. Example

Send a notification to one device, using Base64 encoded device token:

    REQ> send 1343727824 1 oplo1dgXSxYT5jGmD/L3XjVSRHCT1EkMLBk+/xp5HAY= {"aps":{"alert":"hello","aid":"example"}}
    REP> OK 34

Send a notification to two devices, using hex encoded device tokens:

    REQ> send +604800 2 DDE37D652A87B72516D51117B45980852A22CEFAB57ABF84ED11F937ED621007 7B168DB2D8F3EBAE6A0235AFF7CEDAC279ECBA6DDE83099816383E94B3B2020C {"aps":{"alert":"hello","aid":"example"}}
    REP> OK 35 36

Send a notification using an invalid device token format:

    REQ> send +2419200 1 DEADBEEF {"aps":{"alert":"hello","aid":"example"}}
    REP> ERROR Wrong device token length (6 != 32): DEADBEEF
    REQ> send 1343730023 1 oplo1dgXSxYT5jGmD/L3XjVSRHCT1EkMLBk-/xp5HAD= {"aps":{"alert":"hello","aid":"example"}}
    REP> ERROR Wrong Base64 encoding for device token: oplo1dgXSxYT5jGmD/L3XjVSRHCT1EkMLBk-/xp5HAD=

Send a notification using an invalid payload:

    REQ> send 1343727824 1 oplo1dgXSxYT5jmD/L3XjVSRHCT1EkMLBk+/xp5HAY= hello
    REP> ERROR Invalid JSON payload: hello

### III.1.2. `feedback` command

#### III.1.2.1. Request

    REQ = "feedback"

Simple, isn't it?

#### III.1.2.2. Reply

    REP = "OK" { feedback }
    feedback = timestamp:devicetoken

* `timestamp` is a timestamp in the UNIX format provided by APNS,
indicating when the APNS determined that the application no longer
exists on the device.
**If the timestamp is 0**, this means that the APNS thinks it is an invalid
token and you must stop using it and possibly investigate why it is
there.
**Otherwise you have to compare this timestamp to the one recorded in
your token database** in order to determine if the application on the
device has re-registered since then.  If it hasn't, you must stop
sending push notifications to the device.
* `devicetoken` is the affected 32-bytes device token encoded either in
hexadecimal or in Base64, depending on your `device_token_format`
parameter in the configuration file.

#### III.1.2.3. Example

No feedback:

    REQ> feedback
    REP> OK

Feedback containing one erronenous device token (daemon configured to
print device tokens as hex):

    REQ> feedback
    REP> OK 0:7B168DB2D8F3EBAE6A0235AFF7CEDAC279ECBA6DDE83099816383E94B3B2020C

Feedback containing two device tokens that no longer exists (daemon
configured to print device tokens as Base64):

    REQ> feedback
    REP> OK 1344433059:W2EZnh9/mwvjB/AauQ3mQ/wKgAazGc/FwT+omnvv+pk= 1344498238:qnkz8vXkLFQjCtnmnayGV0zazaqEXd9ZGiSR2TY0M0U=

## III.2. GCM

For a better grasp of the whole picture, please read the [GCM
documentation](http://developer.android.com/guide/google/gcm/index.html).

### III.2.1. `send` command

#### III.2.1.1. Request

    REQUEST = "send" collapsekey expiry delayidle count devicetoken { devicetoken } payload
    delayidle = "delayidle" | "nodelayidle"

* `collapsekey` is an arbitrary string (without whitespace) that is used
to collapse a group of messages when the device is offline, so that only
the last message gets sent to the client (quoting [GCM Request format
documentation](http://developer.android.com/guide/google/gcm/gcm.html#request)).
* `expiry` is how long the notification is valid.
It may be an absolute value representing is a UNIX epoch date or,
if it is prefixed with a `+` character, the following number is
added to the current UNIX time to compute the expiry date.  An expiry in
the past will be discarded by the daemon.  An expiry cannot be greater
than four weeks in the future (2419200 seconds) (enforced by GCM).  Note
that the notification may be enqueued for some time (usually a few
seconds) in the daemon before being send to APNS, depending on its load.
* `delayidle` defines if the message should not be sent immediately if
the device is idle ((quoting [GCM Request format
documentation](http://developer.android.com/guide/google/gcm/gcm.html#request)).
* `count` is the number of following device tokens.  GCM enforces that
requests must not address more than 1000 devices, but the daemon will
automatically make multiple requests if needed.
* `devicetoken` is the device token ("registration ID" in GCM language).
* `payload` is what must be sent to the device.  The format must be
valid JSON and must not exceed 4096 bytes (enforced by GCM).  No
further check is done.  This is the only argument that can contain
spaces.

#### III.2.1.2. Reply

    RESPONSE = rep_ok | rep_err
    rep_ok = "OK" id { id }
    rep_err = "ERROR" errormsg

* `id` is the notification identifier.  It is unique for each
notification request sent (one request every 1000 device tokens).
* `errormsg` is an error message describing the problem.

If an error happens while trying to send a notification to multiple
devices, nothing will be pushed at all.

#### III.2.1.3. Example

Send a notification to two device tokens (registration ID in GCM
language):

    REQ> send 1343727824 2 APA91bE01klpKUSdNV7VV-8_kixm4MA9Vn10Hua1-1jGe9GZMXcvCSl1fKUUNmNGTJoPa3thUHEKUEjatJh-Qtlc5xbWlFt23wSfT69a5ucmp4jdXw20KZOVEc6rOaPbqL9aqjCDX16xiGCxU3G2qpBcxvtKEjD8RCyAc-iYQMcq4OxGHvOHXFY ZqmdcEq3sUvs9cl7K8nj48poxSQi15yhECergqmY0_G6Go3EIy0s17X-h35qeABBatPq0j1uS8CYH1Zj_UhHHb8u8kpwFv1iIGYvAAk5WPmBTTosnAV_C85MJ4 {"score":"4x8","time":"15:16.2342"}
    REP> OK 34 35

Send a notification an expiry too far in the future:

    REQ> send +3000000 1 APA91bE01klpKUSdNV7VV-8_kixm4MA9Vn10Hua1-1jGe9GZMXcvCSl1fKUUNmNGTJoPa3thUHEKUEjatJh-Qtlc5xbWlFt23wSfT69a5ucmp4jdXw20KZOVEc6rOaPbqL9aqjCDX16xiGCxU3G2qpBcxvtKEjD8RCyAc-iYQMcq4OxGHvOHXFY {"score":"4x8","time":"15:16.2342"}
    REP> ERROR Expiry value too high (max 2419200s in the future): 3000000

Send a notification using an invalid payload:

    REQ> send 1343727824 1 APA91bE01klpKUSdNV7VV-8_kixm4MA9Vn10Hua1-1jGe9GZMXcvCSl1fKUUNmNGTJoPa3thUHEKUEjatJh-Qtlc5xbWlFt23wSfT69a5ucmp4jdXw20KZOVEc6rOaPbqL9aqjCDX16xiGCxU3G2qpBcxvtKEjD8RCyAc-iYQMcq4OxGHvOHXFY hello
    REP> ERROR Invalid JSON payload: hello

### III.2.2. `feedback` command

#### III.2.2.1. Request

    REQ = "feedback"

Simple, isn't it?

#### III.2.2.2. Reply

    REP = "OK" { feedback }
    feedback = devicetoken:state:newdevicetoken
    state = "replaced" | "notregistered" | "invalid"

* `devicetoken` is the affected device token.
* `state` determines what to do with `devicetoken`.  If "notregistered"
or "invalid" you must remove it from your database and possibly
investigate for the latter.  If "replaced", `devicetoken` must be
replaced with `newdevicetoken` in your database.
* `newdevicetoken` is the device token with which you must replace
`devicetoken` in your database **if `state` is "replaced"**.  Otherwise
it is empty.

#### III.2.2.3. Example

No feedback:

    REQ> feedback
    REP> OK

Feedback containing one unregistered device token and one invalid device
token:
print

    REQ> feedback
    REP> OK APA91bE01klpKUSdNV7VV-8_kixm4MA9Vn10Hua1-1jGe9GZMXcvCSl1fKUUNmNGTJoPa3thUHEKUEjatJh-Qtlc5xbWlFt23wSfT69a5ucmp4jdXw20KZOVEc6rOaPbqL9aqjCDX16xiGCxU3G2qpBcxvtKEjD8RCyAc-iYQMcq4OxGHvOHXFY:unregistered: ZqmdcEq3sUvs9cl7K8nj48poxSQi15yhECergqmY0_G6Go3EIy0s17X-h35qeABBatPq0j1uS8CYH1Zj_UhHHb8u8kpwFv1iIGYvAAk5WPmBTTosnAV_C85MJ4:invalid:

Feedback containing a replaced device token:

    REQ> feedback
    REP> OK
    APA91bE01klpKUSdNV7VV-8_kixm4MA9Vn10Hua1-1jGe9GZMXcvCSl1fKUUNmNGTJoPa3thUHEKUEjatJh-Qtlc5xbWlFt23wSfT69a5ucmp4jdXw20KZOVEc6rOaPbqL9aqjCDX16xiGCxU3G2qpBcxvtKEjD8RCyAc-iYQMcq4OxGHvOHXFY:replaced:ZqmdcEq3sUvs9cl7K8nj48poxSQi15yhECergqmY0_G6Go3EIy0s17X-h35qeABBatPq0j1uS8CYH1Zj_UhHHb8u8kpwFv1iIGYvAAk5WPmBTTosnAV_C85MJ4

# IV. APNS CERTIFICATES

As time of writing, APNS is signed by Entrust.  You can check this using:

    openssl s_client -connect gateway.sandbox.push.apple.com:2195 

This command will show you the certificate used by APNS.  The output is
quite verbose, but you will see a "Certificate chain" section at the
beginning.  Right now it is:

    Certificate chain
       0 s:/C=US/ST=California/L=Cupertino/O=Apple Inc./OU=iTMS Engineering/CN=gateway.sandbox.push.apple.com
         i:/C=US/O=Entrust, Inc./OU=www.entrust.net/rpa is incorporated by reference/OU=(c) 2009 Entrust, Inc./CN=Entrust Certification Authority - L1C
       1 s:/C=US/O=Entrust, Inc./OU=www.entrust.net/rpa is incorporated by reference/OU=(c) 2009 Entrust, Inc./CN=Entrust Certification Authority - L1C
         i:/O=Entrust.net/OU=www.entrust.net/CPS_2048 incorp. by ref. (limits liab.)/OU=(c) 1999 Entrust.net Limited/CN=Entrust.net Certification Authority (2048)

This last line is the root certificate you need to authenticate APNS'
certificate.  On Debian, you can find it in
    /etc/ssl/certs/Entrust.net_Premium_2048_Secure_Server_CA.pem

In case you need it, just ask "Entrust root certificate" to Google and
once you are on the page on Entrust's website, select "Root Certificates"
and download "entrust\_2048\_ca.cer".  This is the same file as above.



# V. DESIGN

Each service has at least two kind of threads:
* a listener threads which receives commands through a ZeroMQ REP/REQ
  socket;
* an agent thread 

The main thread listens on a REP/REQ (ping/pong) ZeroMQ socket for user
commands.  Depending on the configuration file, there are 1 to N threads
connected to the APNS for sending notifications.  There is also one thread
dedicated to the feedback service.

A PersistentQueue class derives the Python's synchronized Queue class but
with a persistent SQLite backend.

Each notification requested by the user through the ZeroMQ socket is
enqueued to the persistent queue of notifications, from which notification
threads retrieve it and relay it to APNS.

The feedback thread periodically connects to the Apple feedback service and
enqueues each message to a persistent queue, which is polled when the user
uses the feedback command through the ZeroMQ socket.
