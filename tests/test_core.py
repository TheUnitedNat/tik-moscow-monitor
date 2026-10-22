import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import storage, scanner
from regions import lookup

class TestCore(unittest.TestCase):
 def test_regions(self):
  self.assertEqual(lookup('Территориальная избирательная комиссия района Солнцево'),'ЗАО')
  self.assertEqual(lookup('Территориальная избирательная комиссия Филимонковского района'),'НАО')
  self.assertEqual(lookup('Территориальная избирательная комиссия района Коммунарка'),'НАО')
 def test_132_tiks(self):
  import json
  names=json.loads((Path(__file__).parent/'official_tik_names_2026.json').read_text())
  self.assertEqual(len(names),132)
  self.assertTrue(all(lookup(x)!='Не определён' for x in names))
 def test_storage(self):
  with tempfile.TemporaryDirectory() as x:
   with patch.object(storage,'DATA_DIR',Path(x)),patch.object(storage,'DB_PATH',Path(x)/'x.db'):
    storage.init_db()
    storage.sub_create(123,'ao','ЗАО','uik')
    storage.sub_create(123,'ao','ЗАО','uik')
    self.assertEqual(len(storage.sub_list(123)),1)
    self.assertTrue(storage.sub_delete(123,storage.sub_list(123)[0]['id']))
    self.assertEqual(storage.sub_list(123),[])
 def test_baseline_then_new(self):
  import mgik_api as api
  with tempfile.TemporaryDirectory() as x:
   with patch.object(storage,'DATA_DIR',Path(x)),patch.object(storage,'DB_PATH',Path(x)/'x.db'):
    storage.init_db()
    old={'pk':1,'name':'Старый УИК документ','date':'2026-10-19'}
    new={'pk':2,'name':'О формировании УИК №4','date':'2026-10-22'}
    with patch.object(scanner.api,'fetch_all',side_effect=[[{'pk':10,'name':'ТИК района Солнцево'}],[old],[{'pk':10,'name':'ТИК района Солнцево'}],[old,new]]),patch.object(scanner.time,'sleep'):
     self.assertTrue(scanner.scan()['baseline'])
     self.assertEqual(scanner.scan()['new'],1)
    storage.sub_create(123,'ao','ЗАО','uik')
    # the decisions were collected before subscribing, no backlog
    self.assertEqual(storage.unsent(storage.sub_list(123)[0]),[])

if __name__=='__main__': unittest.main()
