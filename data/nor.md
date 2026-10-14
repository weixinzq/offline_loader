这是 normallogin.action信息
RequestHeaders

:authority
account.100bt.com
:method
POST
:path
/game/auth/normallogin.action
:scheme
https
accept
application/json, text/plain, */*
accept-encoding
gzip, deflate, br, zstd
accept-language
zh-CN,zh;q=0.9
content-length
155
content-type
application/x-www-form-urlencoded
cookie
ywzc_UUID=e3cd878c-eaf5-4cdd-b544-014ffea659a7; ywzc_new_UUID=d36fb94b-3e62-4c65-997c-6c7ee4539743; mid1=c354204b-25f0-4670-bff3-0308793f5dcd; Hm_lvt_7fc3681c21a26a2022ae0ca72e2d6fa5=1789722734,1791981785; HMACCOUNT=982D5C18814EBDA0; game_auth_web_login_sid=90b38dc0-ed65-4e61-b22d-80ed45dbc724; Hm_lpvt_7fc3681c21a26a2022ae0ca72e2d6fa5=1791981856; his_auth_sid=gameId%3D2%2CgameName%3D%E5%A5%A5%E6%8B%89%E6%98%9F%2CduoduoId%3D112742287%2Ctoken%3D001a7dc418e24b19b0fb98827e0b6ad1%2CautoLogin%3Dtrue%2CQQNickName%3D%2CWXNickName%3D%3BgameId%3D2%2CgameName%3D%E5%A5%A5%E6%8B%89%E6%98%9F%2CduoduoId%3D943352478%2Ctoken%3D0014885657a64c718dbd0914ba86ecf8%2CautoLogin%3Dtrue%2CQQNickName%3D%2CWXNickName%3D
origin
https://baitian-h5-sdk.100bt.com
priority
u=1, i
referer
https://baitian-h5-sdk.100bt.com/
sec-ch-ua
"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"
sec-ch-ua-mobile
?0
sec-ch-ua-platform
"Windows"
sec-fetch-dest
empty
sec-fetch-mode
cors
sec-fetch-site
same-site
user-agent
Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36

payload:
account=112742210&password=e44f8cf63970db5c2df0a18153bcdf49&captcha=&gameId=2&gameName=%E5%A5%A5%E6%8B%89%E6%98%9F&gameEnName=aola&autoLogin=true&nickName=


Response:
{"resultCode":{"code":0,"detail":"成功"},"value":{"needCaptcha":false,"state":0}}


Response Headers:
access-control-allow-credentials
true
access-control-allow-headers
DNT,X-CustomHeader,Keep-Alive,User-Agent,X-Requested-With,If-Modified-Since,Cache-Control,Content-Type
access-control-allow-methods
GET, POST, OPTIONS
access-control-allow-origin
https://baitian-h5-sdk.100bt.com
content-language
zh-CN
content-length
83
content-type
application/json;charset=UTF-8
date
Wed, 14 Oct 2026 12:45:09 GMT
p3p
CP=CURa ADMa DEVa PSAo PSDo OUR BUS UNI PUR INT DEM STA PRE COM NAV OTC NOI DSP COR
set-cookie
his_auth_sid=gameId%3D2%2CgameName%3D%E5%A5%A5%E6%8B%89%E6%98%9F%2CduoduoId%3D112742210%2Ctoken%3D001e7f8f6eaf4330aa387c18d3d4034d%2CautoLogin%3Dtrue%2CQQNickName%3D%2CWXNickName%3D%3BgameId%3D2%2CgameName%3D%E5%A5%A5%E6%8B%89%E6%98%9F%2CduoduoId%3D112742287%2Ctoken%3D001a7dc418e24b19b0fb98827e0b6ad1%2CautoLogin%3Dtrue%2CQQNickName%3D%2CWXNickName%3D%3BgameId%3D2%2CgameName%3D%E5%A5%A5%E6%8B%89%E6%98%9F%2CduoduoId%3D943352478%2Ctoken%3D0014885657a64c718dbd0914ba86ecf8%2CautoLogin%3Dtrue%2CQQNickName%3D%2CWXNickName%3D; Max-Age=2147483647; Expires=Mon, 01-Nov-2094 15:59:16 GMT; Domain=100bt.com; Path=/
set-cookie
game_auth_sid=gameId%3D2%2CgameName%3D%E5%A5%A5%E6%8B%89%E6%98%9F%2CduoduoId%3D112742210%2Ctoken%3D001e7f8f6eaf4330aa387c18d3d4034d%2CautoLogin%3Dtrue%2CQQOpenId%3D%2CQQNickName%3D%2CQQGender%3D%2CQQAvatarUrl%3D%2CWXNickName%3D%2CnickName%3D; Max-Age=7776000; Expires=Tue, 12-Jan-2027 12:45:09 GMT; Domain=100bt.com; Path=/



以下是 newlogin.jsp 

Request Headers :
:authority
login-aola.100bt.com
:method
POST
:path
/newLogin.jsp
:scheme
https
accept
*/*
accept-encoding
gzip, deflate, br, zstd
accept-language
zh-CN,zh;q=0.9
content-length
110
content-type
application/x-www-form-urlencoded
cookie
ywzc_UUID=e3cd878c-eaf5-4cdd-b544-014ffea659a7; ywzc_new_UUID=d36fb94b-3e62-4c65-997c-6c7ee4539743; mid=c354204b-25f0-4670-bff3-0308793f5dcd; mid1=c354204b-25f0-4670-bff3-0308793f5dcd; Hm_lvt_7fc3681c21a26a2022ae0ca72e2d6fa5=1789722734,1791981785; HMACCOUNT=982D5C18814EBDA0; Hm_lpvt_7fc3681c21a26a2022ae0ca72e2d6fa5=1791981856; his_auth_sid=gameId%3D2%2CgameName%3D%E5%A5%A5%E6%8B%89%E6%98%9F%2CduoduoId%3D112742210%2Ctoken%3D001e7f8f6eaf4330aa387c18d3d4034d%2CautoLogin%3Dtrue%2CQQNickName%3D%2CWXNickName%3D%3BgameId%3D2%2CgameName%3D%E5%A5%A5%E6%8B%89%E6%98%9F%2CduoduoId%3D112742287%2Ctoken%3D001a7dc418e24b19b0fb98827e0b6ad1%2CautoLogin%3Dtrue%2CQQNickName%3D%2CWXNickName%3D%3BgameId%3D2%2CgameName%3D%E5%A5%A5%E6%8B%89%E6%98%9F%2CduoduoId%3D943352478%2Ctoken%3D0014885657a64c718dbd0914ba86ecf8%2CautoLogin%3Dtrue%2CQQNickName%3D%2CWXNickName%3D
origin
https://aola.100bt.com
priority
u=1, i
referer
https://aola.100bt.com/
sec-ch-ua
"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"
sec-ch-ua-mobile
?0
sec-ch-ua-platform
"Windows"
sec-fetch-dest
empty
sec-fetch-mode
cors
sec-fetch-site
same-site
user-agent
Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36


payload:
isnickname=sso&account=112742210&token=001e7f8f6eaf4330aa387c18d3d4034d&platform=CN_H5&forceLogin=0&wyToken=-1