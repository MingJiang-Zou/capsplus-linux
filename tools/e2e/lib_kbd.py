"""虚拟键盘测试台公共件（无副作用）"""
import ctypes, fcntl, os, struct, time, glob
UINPUT="/dev/uinput"
UI_SET_EVBIT,UI_SET_KEYBIT=0x40045564,0x40045565
UI_DEV_SETUP,UI_DEV_CREATE,UI_DEV_DESTROY=0x405c5503,0x5501,0x5502
EV_KEY,EV_SYN=1,0
class InputId(ctypes.Structure): _fields_=[("bustype",ctypes.c_uint16),("vendor",ctypes.c_uint16),("product",ctypes.c_uint16),("version",ctypes.c_uint16)]
class UinputSetup(ctypes.Structure): _fields_=[("id",InputId),("name",ctypes.c_char*80),("ff_effects_max",ctypes.c_uint32)]
K=dict(q=16,w=17,z=44,m=50,esc=1,c=46,v=47,x=45,a=30,b=48,d=32,e=18,f=33,g=34,h=35,i=23,j=36,k=37,l=38,
       n=49,o=24,p=25,r=19,s=31,t=20,u=22,y=21,semicolon=39,comma=51,dot=52,slash=53,
       space=57,enter=28,backspace=14,delete=111,left=105,right=106,up=103,down=108,
       home=102,end=107,pageup=104,pagedown=109,leftctrl=29,leftshift=42,leftalt=56,
       capslock=58,f13=183,f14=184,f15=185,f16=186,f17=187,f18=188,f19=189,f20=190,f21=191,f22=192,f23=193,f24=194)
N={v:k for k,v in K.items()}
N[0]="SYN"
def make_vkbd(name=b"echo-e2e-keyboard"):
    fd=os.open(UINPUT,os.O_WRONLY|os.O_NONBLOCK)
    fcntl.ioctl(fd,UI_SET_EVBIT,EV_KEY); fcntl.ioctl(fd,UI_SET_EVBIT,EV_SYN)
    for c in range(1,256):
        try: fcntl.ioctl(fd,UI_SET_KEYBIT,c)
        except OSError: pass
    s=UinputSetup(); s.id=InputId(0x03,0x1234,0x5678,1); s.name=name; s.ff_effects_max=0
    fcntl.ioctl(fd,UI_DEV_SETUP,bytes(s)); fcntl.ioctl(fd,UI_DEV_CREATE)
    return fd
def emit(fd,code,val):
    os.write(fd,struct.pack("llHHi",0,0,EV_KEY,code,val))
    os.write(fd,struct.pack("llHHi",0,0,EV_SYN,0,0))
def find_keyd_kbd():
    for p in sorted(glob.glob("/sys/class/input/event*/device/name")):
        if open(p).read().strip()=="keyd virtual keyboard":
            return "/dev/input/"+p.split("/")[4]
    raise SystemExit("找不到 keyd 虚拟键盘")
class Tap:
    def __init__(self):
        self.fd=os.open(find_keyd_kbd(), os.O_RDONLY|os.O_NONBLOCK)
    def drain(self,dur):
        t0=time.time(); out=[]
        while time.time()-t0<dur:
            try: data=os.read(self.fd,24*256)
            except BlockingIOError: time.sleep(0.004); continue
            for i in range(0,len(data)-len(data)%24,24):
                _,_,t,c,v=struct.unpack("llHHi",data[i:i+24])
                if t==EV_KEY: out.append((round(time.time()-t0,3),c,v))
        return out
    def quiet(self,maxw=8.0):
        t0=time.time()
        while time.time()-t0<maxw:
            if not self.drain(0.45): return True
        return False
    def until_quiet(self,maxw=8.0,quiet=0.5):
        out=[]; t0=time.time(); last=time.time()
        while time.time()-t0<maxw:
            ch=self.drain(0.15)
            if ch: out+=ch; last=time.time()
            elif time.time()-last>quiet: break
        return out
    def close(self): os.close(self.fd)
def composer(vk,keys,hold=0.06):
    for k in keys: emit(vk,K[k],1); time.sleep(hold)
    time.sleep(hold)
    for k in reversed(keys): emit(vk,K[k],0); time.sleep(hold)
