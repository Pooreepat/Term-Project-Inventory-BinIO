#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Inventory-BinIO (clean)
- 3 ไฟล์ไบนารี: categories.bin / items.bin / movements.bin
- fixed-length records + struct (endianness '<') + header 128B + index 16B/slot
- index แบบ open addressing + free-list + soft delete (flag=0)
- เมนู CRUD/VIEW/REPORT (+ ปุ่ม Back ทุกเมนูย่อย)

โมเดล:
- Category : หมวดหมู่พัสดุ
- Item     : รายการพัสดุ (ปริมาณ/ราคา/สถานะ)
- Movement : การเคลื่อนย้าย/ทำรายการ (issue/transfer/return/repair)

หมายเหตุ:
- Movement 'issue'  : ตัดสต็อก (qty -= n) ถ้าพอ
- Movement 'return' : เติมสต็อก (qty += n)
- Movement 'transfer' / 'repair' : ไม่แตะ qty (เก็บประวัติ)
"""
from __future__ import annotations
import os, sys, struct, argparse
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional, Iterable, Tuple, Dict, Any

# ----------------------------
# สเปกไฟล์/บันทัดฐาน
# ----------------------------
E = '<'                               # little-endian
HEADER_SIZE = 128
INDEX_SLOT_SIZE = 16
HEADER_FMT = E + '4s B B H I I I I I i I 92x'   # 128B
INDEX_FMT  = E + 'I I 8x'                        # 16B
TOMBSTONE_KEY = 0xFFFFFFFF

# Record format
# Category: 1+4+30+80=115 -> pad 13, next_free @115
CAT_FMT = E + 'B I 30s 80s 13x'; CAT_SIZE=128; CAT_PAD=115
# Item: 1+4+30+4(cat)+4(qty)+4(price_cents)+1(status)=48 -> pad 80, next_free @48
ITEM_FMT = E + 'B I 30s I I I B 80x'; ITEM_SIZE=128; ITEM_PAD=48
# Movement: 1+4+4+4(ymd)+4(qty)+4(type)+30(op)=51 -> pad 13, next_free @51 (ไม่ใช้ free-list สำหรับ movements ตอนลบก็ได้ แต่รองรับ)
MOVE_FMT = E + 'B I I I I I 30s 13x'; MOVE_SIZE=64; MOVE_PAD=51

ITEM_STATUS = {0:'available', 1:'damaged', 2:'disposed'}
ITEM_STATUS_REV = {v:k for k,v in ITEM_STATUS.items()}

MOVE_TYPE = {0:'issue', 1:'transfer', 2:'return', 3:'repair'}
MOVE_TYPE_REV = {v:k for k,v in MOVE_TYPE.items()}

# ----------------------------
# ยูทิลิตี้
# ----------------------------
now_ts = lambda: int(datetime.now().timestamp())

def fit(s: str, n: int) -> bytes:
    return (s or '').encode('utf-8','ignore')[:n].ljust(n, b'\x00')

def ymd_to_int(s: str) -> int:
    if not s: return 0
    y,m,d = map(int, s.split('-'))
    return y*10000 + m*100 + d

def int_to_ymd(n: int) -> str:
    if not n: return '-'
    y=n//10000; m=(n//100)%100; d=n%100
    return f"{y:04d}-{m:02d}-{d:02d}"

def ensure_dir(p: str) -> None:
    if p and not os.path.isdir(p): os.makedirs(p, exist_ok=True)

# ----------------------------
# Header/Index โครงสร้าง
# ----------------------------
@dataclass
class Header:
    magic: bytes; version: int; endian: int; record_size: int
    created_at: int; updated_at: int; next_id: int
    active_count: int; deleted_count: int; free_head: int; index_slots: int
    def pack(self) -> bytes:
        return struct.pack(HEADER_FMT, self.magic, self.version, self.endian,
                           self.record_size, self.created_at, self.updated_at,
                           self.next_id, self.active_count, self.deleted_count,
                           self.free_head, self.index_slots)
    @classmethod
    def unpack(cls, b: bytes) -> 'Header':
        (magic, ver, ed, rsz, c_at, u_at, nid, ac, dc, fh, slots) = struct.unpack(HEADER_FMT, b)
        return cls(magic,ver,ed,rsz,c_at,u_at,nid,ac,dc,fh,slots)
    @classmethod
    def new(cls, magic: bytes, record_size: int, index_slots: int) -> 'Header':
        t = now_ts(); return cls(magic, 1, 0, record_size, t, t, 1, 0, 0, -1, index_slots)

@dataclass
class IndexSlot:
    key: int; rec_index: int
    def pack(self) -> bytes: return struct.pack(INDEX_FMT, self.key, self.rec_index)
    @classmethod
    def unpack(cls, b: bytes) -> 'IndexSlot':
        k,ri = struct.unpack(INDEX_FMT, b); return cls(k,ri)

# ----------------------------
# ชั้นตารางไบนารี (ทั่วไป)
# ----------------------------
class BinTable:
    def __init__(self, path: str, magic: bytes, rsize: int, rfmt: str, slots: int, pad_off: int):
        self.path=path; self.magic=magic; self.rsize=rsize; self.rfmt=rfmt
        self.slots=slots; self.pad_off=pad_off
        self.f=None; self.h: Optional[Header]=None

    

    # --- file lifecycle ---
    def open(self) -> None:
        new = not os.path.exists(self.path)
        self.f = open(self.path, 'w+b' if new else 'r+b')
        if new:
            self.h = Header.new(self.magic, self.rsize, self.slots)
            self.f.seek(0); self.f.write(self.h.pack())
            for _ in range(self.slots): self.f.write(IndexSlot(0,0).pack())
            self._sync()
        else:
            self.f.seek(0); self.h = Header.unpack(self.f.read(HEADER_SIZE))
            if self.h.magic != self.magic or self.h.record_size != self.rsize:
                raise RuntimeError(f"bad file format for {self.path}")

    def close(self) -> None:
        if self.f: self.f.flush(); os.fsync(self.f.fileno()); self.f.close(); self.f=None

    def _sync(self) -> None:
        self.f.flush(); os.fsync(self.f.fileno())

    def _write_header(self) -> None:
        self.h.updated_at = now_ts(); self.f.seek(0); self.f.write(self.h.pack()); self._sync()

    # --- index helpers ---
    def _index_ofs(self, slot: int) -> int: return HEADER_SIZE + slot*INDEX_SLOT_SIZE
    def _read_slot(self, slot: int) -> IndexSlot:
        self.f.seek(self._index_ofs(slot)); return IndexSlot.unpack(self.f.read(INDEX_SLOT_SIZE))
    def _write_slot(self, slot: int, slotval: IndexSlot) -> None:
        self.f.seek(self._index_ofs(slot)); self.f.write(slotval.pack())
    def _hash(self, key: int) -> int: return key % self.h.index_slots

    def _find_slot_for_insert(self, key: int) -> int:
        start = self._hash(key); first_tomb = -1
        for i in range(self.h.index_slots):
            j = (start + i) % self.h.index_slots
            sl = self._read_slot(j)
            if sl.key == key:
                raise ValueError('duplicate key')
            if sl.key == TOMBSTONE_KEY and first_tomb < 0:
                first_tomb = j
            if sl.key == 0:
                return first_tomb if first_tomb >= 0 else j
        raise RuntimeError('index full')

    def _lookup(self, key: int) -> Optional[int]:
        start = self._hash(key)
        for i in range(self.h.index_slots):
            j = (start + i) % self.h.index_slots
            sl = self._read_slot(j)
            if sl.key == 0:
                return None
            if sl.key == key:
                return sl.rec_index
        return None

    def _slot_of_key(self, key: int) -> Optional[int]:
        start = self._hash(key)
        for i in range(self.h.index_slots):
            j = (start + i) % self.h.index_slots
            sl = self._read_slot(j)
            if sl.key == 0: return None
            if sl.key == key: return j
        return None

    # --- record space ---
    def _records_region_ofs(self) -> int: return HEADER_SIZE + self.h.index_slots*INDEX_SLOT_SIZE
    def _record_ofs(self, rec_index: int) -> int: return self._records_region_ofs() + rec_index*self.rsize
    def _records_count(self) -> int:
        self.f.seek(0, os.SEEK_END)
        payload = self.f.tell() - self._records_region_ofs()
        return 0 if payload <= 0 else payload // self.rsize
    def _read_raw(self, rec_index: int) -> bytes:
        self.f.seek(self._record_ofs(rec_index)); return self.f.read(self.rsize)
    def _write_raw(self, rec_index: int, data: bytes) -> None:
        assert len(data) == self.rsize
        self.f.seek(self._record_ofs(rec_index)); self.f.write(data)
    def _write_next_free(self, rec_index: int, next_free: int) -> None:
        self.f.seek(self._record_ofs(rec_index)+self.pad_off); self.f.write(struct.pack(E+'i', next_free))

    # --- CRUD ขั้นต่ำ ---
    def next_id(self) -> int:
        nid = self.h.next_id; self.h.next_id += 1; self._write_header(); return nid

    def _alloc_rec_index(self) -> int:
        if self.h.free_head != -1:
            i = self.h.free_head
            self.f.seek(self._record_ofs(i) + self.pad_off)
            nxt = struct.unpack(E+'i', self.f.read(4))[0]
            self.h.free_head = nxt
            return i
        return self._records_count()

    def add_record(self, key: int, packed: bytes) -> int:
        i = self._alloc_rec_index(); self._write_raw(i, packed)
        j = self._find_slot_for_insert(key); self._write_slot(j, IndexSlot(key, i))
        self.h.active_count += 1; self._write_header(); self._sync(); return i

    def read_record(self, key: int) -> Optional[bytes]:
        ri = self._lookup(key); return None if ri is None else self._read_raw(ri)

    def update_record(self, key: int, packed: bytes) -> None:
        ri = self._lookup(key)
        if ri is None: raise KeyError('not found')
        self._write_raw(ri, packed); self._write_header(); self._sync()

    def delete_record(self, key: int) -> None:
        ri = self._lookup(key)
        if ri is None: raise KeyError('not found')
        rec = bytearray(self._read_raw(ri)); rec[0] = 0; self._write_raw(ri, bytes(rec))
        self._write_next_free(ri, self.h.free_head); self.h.free_head = ri
        sj = self._slot_of_key(key)
        if sj is not None:
            self._write_slot(sj, IndexSlot(TOMBSTONE_KEY, 0))
        self.h.active_count -= 1; self.h.deleted_count += 1; self._write_header(); self._sync()

    # --- iterators ---
    def iter_active(self) -> Iterable[Tuple[int, bytes]]:
        for i in range(self._records_count()):
            raw = self._read_raw(i)
            if raw and raw[0] == 1: yield i, raw
    def iter_all(self) -> Iterable[Tuple[int, bytes]]:
        for i in range(self._records_count()):
            raw = self._read_raw(i)
            if raw: yield i, raw
    

# ----------------------------
# ตารางเฉพาะ
# ----------------------------
class Categories(BinTable):
    def __init__(self, path: str, slots: int = 512):
        super().__init__(path, b'CATE', CAT_SIZE, CAT_FMT, slots, CAT_PAD)
    def pack(self, flag:int, cid:int, name:str, desc:str) -> bytes:
        return struct.pack(self.rfmt, flag, cid, fit(name,30), fit(desc,80))
    def unpack(self, raw: bytes) -> Dict[str,Any]:
        f,cid,nm,ds = struct.unpack(E+'B I 30s 80s 13x', raw)
        dec=lambda b:b.rstrip(b'\x00').decode('utf-8','ignore')
        return {'flag':f,'cat_id':cid,'name':dec(nm),'desc':dec(ds)}

class Items(BinTable):
    def __init__(self, path: str, slots: int = 4096):
        super().__init__(path, b'ITEM', ITEM_SIZE, ITEM_FMT, slots, ITEM_PAD)
    def pack(self, flag:int, iid:int, name:str, cat_id:int, qty:int, price_cents:int, status:int) -> bytes:
        return struct.pack(self.rfmt, flag, iid, fit(name,30), cat_id, qty, price_cents, status)
    def unpack(self, raw: bytes) -> Dict[str,Any]:
        f,iid,nm,cat,qty,prc,st = struct.unpack(E+'B I 30s I I I B 80x', raw)
        dec=lambda b:b.rstrip(b'\x00').decode('utf-8','ignore')
        return {'flag':f,'item_id':iid,'name':dec(nm),'cat_id':cat,'qty':qty,'price_cents':prc,'status':st}

class Movements(BinTable):
    def __init__(self, path: str, slots: int = 8192):
        super().__init__(path, b'MOVE', MOVE_SIZE, MOVE_FMT, slots, MOVE_PAD)
    def pack(self, flag:int, mid:int, item_id:int, ymd:int, qty:int, typ:int, operator:str) -> bytes:
        return struct.pack(self.rfmt, flag, mid, item_id, ymd, qty, typ, fit(operator,30))
    def unpack(self, raw: bytes) -> Dict[str,Any]:
        f,mid,iid,ymd,qty,typ,op = struct.unpack(E+'B I I I I I 30s 13x', raw)
        dec=lambda b:b.rstrip(b'\x00').decode('utf-8','ignore')
        return {'flag':f,'move_id':mid,'item_id':iid,'ymd':ymd,'qty':qty,'type':typ,'operator':dec(op)}

# ----------------------------
# แอปรายการคำสั่ง (CLI)
# ----------------------------
class App:
    def __init__(self, data_dir: str):
        ensure_dir(data_dir)
        self.cats  = Categories(os.path.join(data_dir, 'categories.bin'))
        self.items = Items(     os.path.join(data_dir, 'items.bin'))
        self.moves = Movements( os.path.join(data_dir, 'movements.bin'))

    # lifecycle
    def open(self): self.cats.open(); self.items.open(); self.moves.open()
    def close(self): self.cats.close(); self.items.close(); self.moves.close()

    # ---------- Add ----------
    def add_category(self):
        name = input('ชื่อหมวด (<=30): ').strip()
        desc = input('คำอธิบาย (<=80): ').strip()
        if not name: print('! ข้อมูลไม่ถูกต้อง'); return
        cid = self.cats.next_id()
        self.cats.add_record(cid, self.cats.pack(1, cid, name, desc))
        print(f'+ เพิ่มหมวด cat_id={cid}')

    def add_item(self):
        name = input('ชื่อพัสดุ (<=30): ').strip()
        try:
            cat_id = int(input('cat_id: ').strip())
            qty    = int(input('จำนวนเริ่มต้น: ').strip())
            priceb = float(input('ราคา/ชิ้น (บาท): ').strip())
        except Exception:
            print('! อินพุตไม่ถูกต้อง'); return
        if not name or qty < 0 or priceb < 0: print('! ข้อมูลไม่ถูกต้อง'); return
        if not self.cats.read_record(cat_id): print('! ไม่พบหมวด'); return
        st_in = (input('สถานะ (available/damaged/disposed) [available]: ').strip().lower() or 'available')
        status = ITEM_STATUS_REV.get(st_in, 0)
        iid = self.items.next_id()
        self.items.add_record(iid, self.items.pack(1, iid, name, cat_id, qty, int(round(priceb*100)), status))
        print(f'+ เพิ่มพัสดุ item_id={iid}')

    def add_movement(self):
        try:
            iid = int(input('item_id: '))
            ymd = ymd_to_int(input('วันที่ (YYYY-MM-DD): ').strip())
            qty = int(input('ปริมาณ: '))
        except Exception:
            print('! อินพุตไม่ถูกต้อง'); return
        if qty <= 0: print('! qty ต้อง > 0'); return
        it_raw = self.items.read_record(iid)
        if not it_raw: print('! ไม่พบพัสดุ'); return
        it = self.items.unpack(it_raw)
        typ_str = (input('ประเภท (issue/transfer/return/repair): ').strip().lower())
        typ = MOVE_TYPE_REV.get(typ_str, None)
        if typ is None: print('! ประเภทไม่ถูกต้อง'); return
        operator = input('ผู้ดำเนินการ (<=30): ').strip()
        # ปรับ qty ตามประเภท
        new_qty = it['qty']
        if typ == 0:  # issue
            if qty > it['qty']:
                print('! สต็อกไม่พอ'); return
            new_qty -= qty
        elif typ == 2:  # return
            new_qty += qty
        # transfer/repair: ไม่แตะ qty
        mid = self.moves.next_id()
        self.moves.add_record(mid, self.moves.pack(1, mid, iid, ymd, qty, typ, operator))
        if new_qty != it['qty']:
            self.items.update_record(iid, self.items.pack(1, it['item_id'], it['name'], it['cat_id'], new_qty, it['price_cents'], it['status']))
        print(f'+ บันทึกการเคลื่อนย้าย move_id={mid}')

    # ---------- Update ----------
    def update_category(self):
        try: cid = int(input('cat_id: '))
        except Exception: print('! อินพุตไม่ถูกต้อง'); return
        raw = self.cats.read_record(cid)
        if not raw: print('! ไม่พบหมวด'); return
        r = self.cats.unpack(raw)
        name = input(f"ชื่อหมวด [{r['name']}]: ").strip() or r['name']
        desc = input(f"คำอธิบาย [{r['desc']}]: ").strip() or r['desc']
        self.cats.update_record(cid, self.cats.pack(1, cid, name, desc))
        print('* อัปเดตหมวดแล้ว')

    def update_item(self):
        try: iid = int(input('item_id: '))
        except Exception: print('! อินพุตไม่ถูกต้อง'); return
        raw = self.items.read_record(iid)
        if not raw: print('! ไม่พบพัสดุ'); return
        r = self.items.unpack(raw)
        name = input(f"ชื่อพัสดุ [{r['name']}]: ").strip() or r['name']
        try:
            cat_id = int(input(f"cat_id [{r['cat_id']}]: ") or r['cat_id'])
            qty    = int(input(f"จำนวน [{r['qty']}]: ") or r['qty'])
            priceb = float(input(f"ราคา/ชิ้น [{r['price_cents']/100:.2f}]: ") or (r['price_cents']/100))
        except Exception:
            print('! ตัวเลขไม่ถูกต้อง'); return
        st_in = input(f"สถานะ ({'/'.join(ITEM_STATUS.values())}) [{ITEM_STATUS[r['status']]}]: ").strip() or ITEM_STATUS[r['status']]
        status = ITEM_STATUS_REV.get(st_in.lower(), r['status'])
        if qty < 0 or priceb < 0: print('! ข้อมูลไม่ถูกต้อง'); return
        if not self.cats.read_record(cat_id): print('! ไม่พบหมวด'); return
        self.items.update_record(iid, self.items.pack(1, iid, name, cat_id, qty, int(round(priceb*100)), status))
        print('* อัปเดตพัสดุแล้ว')

    # ---------- Delete ----------
    def delete_category(self):
        try: cid = int(input('cat_id: '))
        except Exception: print('! อินพุตไม่ถูกต้อง'); return
        # กันลบถ้ายังมี item อ้างอิง
        for _, raw in self.items.iter_active():
            it = self.items.unpack(raw)
            if it['cat_id'] == cid:
                print('! มีพัสดุอ้างอิงหมวดนี้ ลบไม่ได้')
                return
        try: self.cats.delete_record(cid); print('- ลบหมวดแล้ว')
        except Exception as e: print('!', e)

    def delete_item(self):
        try: iid = int(input('item_id: '))
        except Exception: print('! อินพุตไม่ถูกต้อง'); return
        try: self.items.delete_record(iid); print('- ลบพัสดุแล้ว')
        except Exception as e: print('!', e)

    def delete_movement(self):
        try: mid = int(input('move_id: '))
        except Exception: print('! อินพุตไม่ถูกต้อง'); return
        try: self.moves.delete_record(mid); print('- ลบรายการเคลื่อนย้ายแล้ว (ไม่ย้อน qty)')
        except Exception as e: print('!', e)

    # ---------- View ----------
    def view_single(self):
        t = input('ชนิด (category/item/movement): ').strip().lower()
        try: i = int(input('id: '))
        except Exception: print('! อินพุตไม่ถูกต้อง'); return
        if t.startswith('cat'):
            raw = self.cats.read_record(i)
            if not raw: print('! ไม่พบ'); return
            r = self.cats.unpack(raw)
            print(f"[Category] id={r['cat_id']} name={r['name']} desc={r['desc']}")
        elif t.startswith('item'):
            raw = self.items.read_record(i)
            if not raw: print('! ไม่พบ'); return
            r = self.items.unpack(raw)
            print(f"[Item] id={r['item_id']} name={r['name']} cat_id={r['cat_id']} qty={r['qty']} "
                  f"price={r['price_cents']/100:.2f} status={ITEM_STATUS[r['status']]}")
        else:
            raw = self.moves.read_record(i)
            if not raw: print('! ไม่พบ'); return
            r = self.moves.unpack(raw)
            print(f"[Move] id={r['move_id']} item_id={r['item_id']} date={int_to_ymd(r['ymd'])} "
                  f"type={MOVE_TYPE[r['type']]} qty={r['qty']} by={r['operator']}")

    def view_all(self):
        t = input('ชนิด (category/item/movement, 0=Back): ').strip().lower()
        if t in ('', '0', 'b', 'back'):
            return
        if t.startswith('cat'):
            for _, raw in self.cats.iter_active():
                r = self.cats.unpack(raw)
                print(f"{r['cat_id']:>4} | {r['name']:<30} | {r['desc']}")
        elif t.startswith('item'):
            for _, raw in self.items.iter_active():
                r = self.items.unpack(raw)
                print(f"{r['item_id']:>4} | {r['name']:<30} | cat={r['cat_id']:<4} | "
                      f"qty={r['qty']:<6} | {r['price_cents']/100:>8.2f} | {ITEM_STATUS[r['status']]:<8}")
        elif t.startswith('move'):
            for _, raw in self.moves.iter_active():
                r = self.moves.unpack(raw)
                print(f"{r['move_id']:>5} | item={r['item_id']:<4} | {int_to_ymd(r['ymd'])} | "
                      f"{MOVE_TYPE[r['type']]:<8} | qty={r['qty']:<6} | by={r['operator']}")
        else:
            print("เขียนไม่ถูกต้อง")

    def view_filter(self):
        t = input('ชนิด (category/item/movement, 0=Back): ').strip().lower()
        if t in ('', '0', 'b', 'back'):
            return
        if t.startswith('cat'):
            q = input('ค้นหาชื่อหมวด: ').strip().lower()
            for _, raw in self.cats.iter_active():
                r = self.cats.unpack(raw)
                if q in r['name'].lower():
                    print(f"{r['cat_id']:>4} | {r['name']}")
        elif t.startswith('item'):
            raw_in = input('สถานะ (available/damaged/disposed หรือเว้นว่าง): ').strip().lower()
            st_code = None
            if raw_in:
                if raw_in.isdigit() and int(raw_in) in ITEM_STATUS:
                    st_code = int(raw_in)
                else:
                    exact = [k for k,v in ITEM_STATUS.items() if v == raw_in]
                    if exact: st_code = exact[0]
                    else:
                        matched = [k for k,v in ITEM_STATUS.items() if v.startswith(raw_in)]
                        if len(matched)==1: st_code = matched[0]
                        elif len(matched)>1:
                            print('กำกวม:', ', '.join(ITEM_STATUS[m] for m in matched)); return
            cat_in = input('กรอง cat_id (เว้นว่าง=ทั้งหมด): ').strip()
            cat_id = int(cat_in) if cat_in.isdigit() else None
            name_q = input('ค้นหาชื่อพัสดุ (เว้นว่าง=ไม่กรอง): ').strip().lower()
            for _, raw in self.items.iter_active():
                r = self.items.unpack(raw)
                if (st_code is None or r['status']==st_code) and \
                   (cat_id is None or r['cat_id']==cat_id) and \
                   (not name_q or name_q in r['name'].lower()):
                    print(f"{r['item_id']:>4} | {r['name']:<30} | cat={r['cat_id']:<4} | "
                          f"qty={r['qty']:<6} | {r['price_cents']/100:>8.2f} | {ITEM_STATUS[r['status']]:<8}")
        elif t.startswith('move'):
            try:
                a_str,b_str = input('ช่วงวันที่ FROM,TO (YYYY-MM-DD,YYYY-MM-DD): ').split(',')
                a,b = ymd_to_int(a_str.strip()), ymd_to_int(b_str.strip())
            except Exception:
                print('รูปแบบวันที่ไม่ถูกต้อง'); return
            item_in = input('item_id (เว้นว่าง=ทั้งหมด): ').strip()
            item_id = int(item_in) if item_in.isdigit() else None
            for _, raw in self.moves.iter_active():
                r = self.moves.unpack(raw)
                if a <= r['ymd'] <= b and (item_id is None or r['item_id']==item_id):
                    print(f"{r['move_id']:>5} | item={r['item_id']:<4} | {int_to_ymd(r['ymd'])} | "
                          f"{MOVE_TYPE[r['type']]:<8} | qty={r['qty']:<6} | by={r['operator']}")
        else:
            print("เขียนไม่ถูกต้อง")

    def view_stats(self):
        # นับสินค้าตามสถานะ
        cnt = {k:0 for k in ITEM_STATUS}
        total_qty = 0
        total_value = 0
        for _, raw in self.items.iter_active():
            r = self.items.unpack(raw)
            cnt[r['status']] += 1
            total_qty += r['qty']
            total_value += r['qty'] * r['price_cents']
        print('Items by status:')
        for k,v in cnt.items(): print(f"  {ITEM_STATUS[k]:<8} = {v}")
        print(f"Total Qty = {total_qty}")
        print(f"Total Value = {total_value/100:,.2f} THB")

    # ---------- Report ----------
    def generate_report(self, out_path: str):
        # cache ชื่อหมวด
        cat_name = {}
        for _, raw in self.cats.iter_active():
            c = self.cats.unpack(raw)
            cat_name[c['cat_id']] = c['name'] or f"cat#{c['cat_id']}"

        lines=[]
        ts=datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S (%z)')
        lines+=[
            'Inventory System — Summary Report',
            f'Generated At : {ts}',
            'App Version  : 1.0',
            'Endianness   : Little-Endian',
            'Encoding     : UTF-8 (fixed-length)',
            ''
        ]
        th=f"{'ItemID':>6} | {'Name':<30} | {'Cat':<12} | {'Qty':>6} | {'Price':>10} | {'Record':<7} | {'Status':<8}"
        lines+=[th,'-'*len(th)]
        total=active=deleted=0
        total_qty=0; total_val=0
        by_cat_count: Dict[str,int]={}
        by_cat_qty: Dict[str,int]={}
        by_cat_val: Dict[str,int]={}
        for _,raw in self.items.iter_all():
            total+=1
            it=self.items.unpack(raw)
            is_active=(raw[0]==1)
            record_state='Active' if is_active else 'Deleted'
            cat = cat_name.get(it['cat_id'], f"cat#{it['cat_id']}")
            lines.append(f"{it['item_id']:>6} | {it['name']:<30.30} | {cat:<12.12} | "
                         f"{it['qty']:>6} | {it['price_cents']/100:>10.2f} | {record_state:<7} | {ITEM_STATUS[it['status']]:<8}")
            if is_active:
                active+=1
                total_qty += it['qty']
                total_val += it['qty'] * it['price_cents']
                by_cat_count[cat]=by_cat_count.get(cat,0)+1
                by_cat_qty[cat]=by_cat_qty.get(cat,0)+it['qty']
                by_cat_val[cat]=by_cat_val.get(cat,0)+it['qty']*it['price_cents']
        deleted=total-active
        lines+=['',
                'Summary (เฉพาะ Active)',
                f'- Total Items (records) : {total}',
                f'- Active Items          : {active}',
                f'- Deleted Items         : {deleted}',
                f'- Total Quantity        : {total_qty}',
                f'- Total Value           : {total_val/100:,.2f} THB',
                '']
        # แจกแจงตามหมวด
        lines.append('Items by Category (Active only)')
        if by_cat_count:
            for cat in sorted(by_cat_count):
                lines.append(f"- {cat:<12} : {by_cat_count[cat]} items, qty={by_cat_qty[cat]}, value={by_cat_val[cat]/100:,.2f} THB")
        else:
            lines.append('(no active items)')
        # Recent movements (ล่าสุด 10)
        recents=[]
        for _,raw in self.moves.iter_active():
            m=self.moves.unpack(raw); recents.append(m)
        recents.sort(key=lambda x: (x['ymd'], x['move_id']), reverse=True)
        lines+=['','Recent Movements (latest 10)']
        if recents:
            lines.append(f"{'MoveID':>6} | {'Date':<10} | {'ItemID':>6} | {'Name':<20} | {'Type':<8} | {'Qty':>6} | {'By':<20}")
            lines.append('-'*86)
            name_cache={}
            def item_name(iid:int)->str:
                if iid in name_cache: return name_cache[iid]
                raw=self.items.read_record(iid)
                name_cache[iid]=self.items.unpack(raw)['name'] if raw else f"item#{iid}"
                return name_cache[iid]
            for m in recents[:10]:
                lines.append(f"{m['move_id']:>6} | {int_to_ymd(m['ymd']):<10} | {m['item_id']:>6} | "
                             f"{item_name(m['item_id']):<20.20} | {MOVE_TYPE[m['type']]:<8} | "
                             f"{m['qty']:>6} | {m['operator']:<20.20}")
        else:
            lines.append('(none)')
        with open(out_path,'w',encoding='utf-8') as f: f.write('\n'.join(lines)+'\n')
        print('* เขียนรายงานที่', out_path)

    # ---------- Menu ----------
    def run(self):
        while True:
            print("\n===== Inventory-BinIO =====")
            print("1) Add  \n2) Update  \n3) Delete  \n4) View  \n5) Report  \n0) Exit")
            c = (input('เลือก: ') or '0').strip().lower()
            try:
                if c == '1':
                    while True:
                        print("\n[Add] \n1) Category \n2) Item \n3) Movement  \n0) Back")
                        ch = input('เลือก: ').strip().lower()
                        if ch in ('0', 'b', 'back'): break
                        {'1': self.add_category,
                         '2': self.add_item,
                         '3': self.add_movement}.get(ch, lambda: print('ตัวเลือกไม่ถูกต้อง'))()
                elif c == '2':
                    while True:
                        print("\n[Update] \n1) Category \n2) Item  \n0) Back")
                        ch = input('เลือก: ').strip().lower()
                        if ch in ('0', 'b', 'back'): break
                        {'1': self.update_category,
                         '2': self.update_item}.get(ch, lambda: print('ตัวเลือกไม่ถูกต้อง'))()
                elif c == '3':
                    while True:
                        print("\n[Delete] \n1) Category \n2) Item \n3) Movement  \n0) Back")
                        ch = input('เลือก: ').strip().lower()
                        if ch in ('0', 'b', 'back'): break
                        {'1': self.delete_category,
                         '2': self.delete_item,
                         '3': self.delete_movement}.get(ch, lambda: print('ตัวเลือกไม่ถูกต้อง'))()
                elif c == '4':
                    while True:
                        print("\n[View] \n1) เดี่ยว \n2) ทั้งหมด \n3) กรอง \n4) สถิติ  \n0) Back")
                        ch = input('เลือก: ').strip().lower()
                        if ch in ('0', 'b', 'back'): break
                        {'1': self.view_single,
                         '2': self.view_all,
                         '3': self.view_filter,
                         '4': self.view_stats}.get(ch, lambda: print('ตัวเลือกไม่ถูกต้อง'))()
                elif c == '5':
                    out = os.path.join(os.path.dirname(self.cats.path), 'inventory_report.txt')
                    self.generate_report(out)
                elif c == '0':
                    out = os.path.join(os.path.dirname(self.cats.path), 'inventory_report.txt')
                    self.generate_report(out)
                    print('บันทึกและออก...')
                    self.close()
                    break
                else:
                    print('ตัวเลือกไม่ถูกต้อง')
            except Exception as e:
                print('! error:', e)

# ----------------------------
# main
# ----------------------------
def main(argv=None) -> int:
    ap=argparse.ArgumentParser(description='Inventory-BinIO (clean)')
    ap.add_argument('--data-dir', default='data_inv', help='โฟลเดอร์เก็บ .bin/.txt')
    args=ap.parse_args(argv)
    ensure_dir(args.data_dir)
    app=App(args.data_dir)
    try:
        app.open(); app.run(); return 0
    finally:
        app.close()

if __name__=='__main__':
    sys.exit(main())