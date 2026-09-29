"""Run with: python -m unittest discover -s tests/unit -v
Requires app dependencies plus httpx. Storage doubles, no Docker needed.
"""
from datetime import datetime, timezone
import hashlib
import io
import os
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4
import psycopg
from fastapi.testclient import TestClient
os.environ.setdefault("SESSION_SECRET", "test-secret-not-for-production")
from app import auth as core_auth
from app.main import app
from app.services import documents as service
from app.repositories import documents as docs, projects
from app.api import health

class ObjectResponse(io.BytesIO):
    def release_conn(self): pass

class Cases(unittest.TestCase):
    def setUp(self):
        self.projects, self.rows, self.metadata, self.objects = {}, {}, {}, {}
        self.fail_mongo = self.fail_minio = self.fail_finish = False
        self.events = []
        def create_project(payload):
            row = dict(id=uuid4(),name=payload.name,description=payload.description,created_at=datetime.now(timezone.utc))
            self.projects[row['id']] = row
            return row.copy()
        def pending(did,pid,name,key,mime,size):
            self.rows[did] = dict(id=did,project_id=pid,original_name=name,object_name=key,content_type=mime,size_bytes=size,status='pending',created_at=datetime.now(timezone.utc))
        def ready(did):
            self.rows[did]['status']='ready'
            return self.rows[did].copy()
        def begin(did):
            self.events.append('intent')
            row = self.rows.get(did)
            if row and row['status'] != 'pending':
                row['status']='deleting'
                return row.copy()
        def finish(did):
            self.events.append('sql-delete')
            if self.fail_finish: raise psycopg.OperationalError('simulated')
            self.rows.pop(did,None)
        def delete_metadata(did):
            self.events.append('mongo-delete')
            if self.fail_mongo: raise RuntimeError('simulated')
            self.metadata.pop(str(did),None)
        def get_metadata(did):
            if self.fail_mongo: raise RuntimeError('simulated')
            return self.metadata.get(str(did))
        def failed(did):
            if did in self.rows and self.rows[did]['status']=='pending': self.rows[did]['status']='failed'
        patches = [
            patch.object(projects,'create_project',create_project),
            patch.object(projects,'get_project',lambda pid:self.projects.get(pid)),
            patch.object(projects,'list_projects',lambda limit,offset,query=None:list(self.projects.values())[offset:offset+limit]),
            patch.object(projects,'count_projects',lambda query=None:len(self.projects)),
            patch.object(docs,'project_exists',lambda pid:self.projects.get(pid)),
            patch.object(docs,'get_document',lambda did:self.rows.get(did,{}).copy() or None),
            patch.object(docs,'create_pending',pending),patch.object(docs,'mark_ready',ready),
            patch.object(docs,'mark_failed',failed),
            patch.object(docs,'list_documents',lambda pid,limit,offset:[r.copy() for r in self.rows.values() if r['project_id']==pid][offset:offset+limit]),
            patch.object(docs,'insert_details',lambda row:self.metadata.update({row['document_id']:row.copy()})),
            patch.object(docs,'get_details',get_metadata),patch.object(docs,'delete_details',delete_metadata),
            patch.object(docs,'begin_delete',begin),patch.object(docs,'finish_delete',finish),
            patch.object(service,'minio_client',lambda:self),patch.object(service,'bucket_name',lambda:'test'),
        ]
        for name in ('check_postgres','check_mongodb','check_minio'):
            patches.append(patch.object(health,name,lambda:None))
        for p in patches: p.start(); self.addCleanup(p.stop)
        self.client=TestClient(app)
        self.addCleanup(self.client.close)
        # This class tests document/project flows, not auth — log in once as
        # a fixed admin session so every request below is already authenticated.
        self.client.cookies.set(core_auth.SESSION_COOKIE, core_auth.create_session_token(uuid4(), 'tester', 'admin'))
        r=self.client.post('/projects',json={'name':'Cloud project'})
        self.assertEqual(r.status_code,201)
        self.pid=r.json()['id']
    def put_object(self,bucket,key,stream,size,**kwargs): self.objects[key]=stream.read()
    def get_object(self,bucket,key): return ObjectResponse(self.objects[key])
    def remove_object(self,bucket,key):
        self.events.append('object-delete')
        if self.fail_minio: raise RuntimeError('simulated')
        self.objects.pop(key,None)
    def upload(self):
        r=self.client.post(f'/ui/projects/{self.pid}/documents',files={'file':('paper.txt',b'original bytes','text/plain')},data={'tags':'cloud,docker','authors':'Dat','custom_metadata':'{"year":2026}'},follow_redirects=False)
        self.assertEqual(r.status_code,303,r.text)
        did=r.headers['location'].rsplit('/',1)[-1]
        return self.client.get('/documents/'+did).json()
    def test_browser_flow_and_hash(self):
        self.assertEqual(self.client.get('/').status_code,200)
        self.assertEqual(self.client.get('/static/style.css').status_code,200)
        r=self.client.post('/ui/projects',data={'name':'UI project'},follow_redirects=False)
        self.assertEqual(r.status_code,303)
        self.assertIn('UI project',self.client.get(r.headers['location']).text)
        doc=self.upload()
        self.assertIn('paper.txt',self.client.get('/ui/documents/'+doc['id']).text)
        r=self.client.get('/documents/'+doc['id']+'/download')
        self.assertEqual(hashlib.sha256(r.content).hexdigest(),doc['sha256'])
        self.assertIn('paper.txt',r.headers['content-disposition'])
        self.assertEqual(doc['custom_metadata'],{'year':2026})
    def test_delete_confirmation_and_idempotency(self):
        doc=self.upload(); did=doc['id']; url='/ui/documents/'+did+'/delete'
        self.assertEqual(self.client.get(url).status_code,200)
        self.assertIn(UUID(did),self.rows)
        self.assertEqual(self.client.post(url,data={}).status_code,422)
        self.assertEqual(self.client.post(url,data={'confirm':'delete'},follow_redirects=False).status_code,303)
        self.assertEqual(self.events,['intent','object-delete','mongo-delete','sql-delete'])
        self.assertNotIn(UUID(did),self.rows);self.assertNotIn(did,self.metadata);self.assertNotIn(doc['object_name'],self.objects)
        self.assertEqual(self.client.get('/documents/'+did).status_code,404)
        self.assertEqual(self.client.delete('/documents/'+did).status_code,200)
    def test_mongo_failure_then_retry(self):
        doc=self.upload();did=doc['id'];self.fail_mongo=True
        self.assertEqual(self.client.delete('/documents/'+did).status_code,503)
        self.assertEqual(self.rows[UUID(did)]['status'],'deleting')
        self.assertNotIn(doc['object_name'],self.objects)
        self.assertEqual(self.client.get('/documents/'+did+'/download').status_code,409)
        self.assertIn('Thử xóa lại',self.client.get('/ui/projects/'+self.pid).text)
        self.fail_mongo=False
        self.assertEqual(self.client.delete('/documents/'+did).status_code,200)
        self.assertNotIn(UUID(did),self.rows)
    def test_minio_failure_retains_metadata(self):
        doc=self.upload();self.fail_minio=True
        self.assertEqual(self.client.delete('/documents/'+doc['id']).status_code,503)
        self.assertIn(doc['id'],self.metadata);self.assertIn(doc['object_name'],self.objects)
        self.fail_minio=False
        self.assertEqual(self.client.delete('/documents/'+doc['id']).status_code,200)
    def test_sql_finish_failure_retry(self):
        doc=self.upload();self.fail_finish=True
        self.assertEqual(self.client.delete('/documents/'+doc['id']).status_code,503)
        self.assertEqual(self.rows[UUID(doc['id'])]['status'],'deleting')
        self.fail_finish=False
        self.assertEqual(self.client.delete('/documents/'+doc['id']).status_code,200)
    def test_pending_is_not_deleted(self):
        doc=self.upload(); self.rows[UUID(doc['id'])]['status']='pending'
        self.assertEqual(self.client.delete('/documents/'+doc['id']).status_code,409)
        self.assertIn(doc['id'],self.metadata);self.assertIn(doc['object_name'],self.objects)
    def test_html_errors_and_json_api_preserved(self):
        for path,status in [('/ui/documents/not-uuid',422),('/ui/documents/'+str(uuid4()),404)]:
            r=self.client.get(path);self.assertEqual(r.status_code,status);self.assertIn('text/html',r.headers['content-type'])
        r=self.client.get('/documents/not-uuid');self.assertEqual(r.status_code,422);self.assertIn('application/json',r.headers['content-type'])
        self.assertEqual(self.client.post('/ui/projects',data={'name':'   '}).status_code,422)
        self.assertEqual(self.client.post('/ui/projects/'+self.pid+'/documents').status_code,422)
        self.assertEqual(self.client.post('/ui/projects/'+self.pid+'/documents',files={'file':('bad.exe',b'x')}).status_code,415)
        self.assertEqual(self.client.post('/ui/projects/'+self.pid+'/documents',files={'file':('big.txt',b'x'*(10*1024*1024+1))}).status_code,413)
        doc=self.upload();self.fail_mongo=True
        r=self.client.get('/ui/documents/'+doc['id']);self.assertEqual(r.status_code,503);self.assertNotIn('Traceback',r.text)
    def test_escaping_and_cross_origin_form(self):
        r=self.client.post('/ui/projects',data={'name':'<script>alert(1)</script>'},follow_redirects=False)
        html=self.client.get(r.headers['location']).text
        self.assertNotIn('<script>alert(1)</script>',html);self.assertIn('&lt;script&gt;',html)
        self.assertEqual(self.client.post('/ui/projects',data={'name':'no'},headers={'Origin':'https://other.invalid'}).status_code,403)
        self.assertEqual(self.client.post('/ui/projects',data={'name':'ok'},headers={'Origin':'http://testserver'},follow_redirects=False).status_code,303)
if __name__=='__main__': unittest.main()
