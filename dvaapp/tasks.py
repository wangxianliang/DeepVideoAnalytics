from __future__ import absolute_import
import subprocess,sys,shutil,os,glob,time,logging
from django.conf import settings
from dva.celery import app
from .models import Video, Frame, Detection, TEvent, Query, IndexEntries,QueryResults, FrameLabel
from dvalib import entity
from dvalib import detector
from dvalib import indexer
from dvalib import facerecognition
from PIL import Image
import json
import celery
import zipfile


def process_video_next(video_id,current_task_name):
    if current_task_name in settings.POST_OPERATION_TASKS:
        for k in settings.POST_OPERATION_TASKS[current_task_name]:
            app.send_task(k,args=[video_id,],queue=settings.TASK_NAMES_TO_QUEUE[k])


class IndexerTask(celery.Task):
    _frame_indexer = None
    _detection_indexer = None

    @property
    def frame_indexer(self):
        if self._frame_indexer is None:
            if 'ALEX_ENABLE' in os.environ:
                self._frame_indexer = {
                    'alex': indexer.AlexnetIndexer(),
                    'inception': indexer.InceptionIndexer(),
                }
            else:
                self._frame_indexer = {
                    'inception': indexer.InceptionIndexer(),
                }
        return self._frame_indexer

    @property
    def detection_indexer(self):
        if self._detection_indexer is None:
            self._detection_indexer = {
                'facenet':indexer.FacenetIndexer(),
            }
        return self._detection_indexer

    def refresh_all_frame_indexes(self):
        index_entries = IndexEntries.objects.all()
        for index_name, visual_index in self.frame_indexer.iteritems():
            for entry in index_entries:
                if entry.video_id not in visual_index.indexed_videos:
                    fname = "{}/{}/indexes/{}.npy".format(settings.MEDIA_ROOT, entry.video_id, index_name)
                    vectors = indexer.np.load(fname)
                    vector_entries = json.load(file(fname.replace(".npy", ".json")))
                    logging.info("Starting {} in {}".format(entry.video_id,visual_index.name))
                    visual_index.load_index(vectors, vector_entries)
                    visual_index.indexed_videos.add(entry.video_id)
                    logging.info("finished {} in {}, current shape {}".format(entry.video_id,visual_index.name,visual_index.index.shape))

    def refresh_detection_index(self,index_name):
        index_entries = IndexEntries.objects.all()
        visual_index = self.detection_indexer[index_name]
        for index_entry in index_entries:
            if index_entry.video_id not in index_entry.indexed_dirs and index_entry.algorithm == index_name:
                fname = "{}/{}/indexes/{}.npy".format(settings.MEDIA_ROOT, index_entry.video_id, index_name)
                vectors = indexer.np.load(fname)
                vector_entries = json.load(file(fname.replace(".npy", ".json")))
                logging.info("Starting {} in {}".format(index_entry.video_id, visual_index.name))
                visual_index.load_index(vectors, vector_entries)
                visual_index.indexed_videos.add(index_entry.video_id)
                logging.info("finished {} in {}, current shape {}".format(index_entry.video_id, visual_index.name,visual_index.index.shape))


@app.task(name="index_by_id",base=IndexerTask)
def perform_indexing(video_id):
    start = TEvent()
    start.video_id = video_id
    start.started = True
    start.operation = perform_indexing.name
    start.save()
    start_time = time.time()
    dv = Video.objects.get(id=video_id)
    video = entity.WVideo(dv, settings.MEDIA_ROOT)
    frames = Frame.objects.all().filter(video=dv)
    for index_name,index_results in video.index_frames(frames,perform_indexing.frame_indexer).iteritems():
        i = IndexEntries()
        i.video = dv
        i.count = len(index_results)
        i.contains_frames = True
        i.detection_name = 'Frame'
        i.algorithm = index_name
        i.save()
    process_video_next(video_id, start.operation)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()


@app.task(name="query_by_id",base=IndexerTask)
def query_by_image(query_id):
    dq = Query.objects.get(id=query_id)
    start = TEvent()
    start.video_id = Video.objects.get(parent_query=dq).pk
    start.started = True
    start.operation = query_by_image.name
    start.save()
    start_time = time.time()
    query_by_image.refresh_all_frame_indexes()
    Q = entity.WQuery(dquery=dq, media_dir=settings.MEDIA_ROOT,frame_indexers=query_by_image.frame_indexer,detection_indexers=query_by_image.detection_indexer)
    results = Q.find(10)
    dq.results = True
    dq.results_metadata = json.dumps(results)
    for algo,rlist in results.iteritems():
        for r in rlist:
            qr = QueryResults()
            qr.query = dq
            qr.frame_id = r['frame_primary_key']
            qr.video_id = r['video_primary_key']
            qr.algorithm = algo
            qr.rank = r['rank']
            qr.distance = r['dist']
            qr.save()
    dq.save()
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    return results


@app.task(name="query_face_by_id",base=IndexerTask)
def query_face_by_image(query_id):
    dq = Query.objects.get(id=query_id)
    start = TEvent()
    start.video_id = Video.objects.get(parent_query=dq).pk
    start.started = True
    start.operation = query_face_by_image.name
    start.save()
    start_time = time.time()
    Q = entity.WQuery(dquery=dq, media_dir=settings.MEDIA_ROOT,frame_indexers=query_face_by_image.frame_indexer,detection_indexers=query_face_by_image.detection_indexer)
    query_face_by_image.refresh_detection_index('facenet')
    results = Q.find_face(10)
    for algo,rlist in results.iteritems():
        for r in rlist:
            qr = QueryResults()
            qr.query = dq
            dd = Detection.objects.get(pk=r['detection_primary_key'])
            qr.detection = dd
            qr.frame_id = dd.frame_id
            qr.video_id = r['video_primary_key']
            qr.algorithm = algo
            qr.rank = r['rank']
            qr.distance = r['dist']
            qr.save()
    dq.results = True
    dq.save()
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    return results


@app.task(name="extract_frames_by_id")
def extract_frames(video_id):
    start = TEvent()
    start.video_id = video_id
    start.started = True
    start.operation = extract_frames.name
    start.save()
    start_time = time.time()
    dv = Video.objects.get(id=video_id)
    v = entity.WVideo(dvideo=dv, media_dir=settings.MEDIA_ROOT)
    time.sleep(3) # otherwise ffprobe randomly fails
    if not dv.dataset:
        v.get_metadata()
        dv.metadata = v.metadata
        dv.length_in_seconds = v.duration
        dv.height = v.height
        dv.width = v.width
        dv.save()
    frames = v.extract_frames()
    dv.frames = len(frames)
    dv.save()
    for f in frames:
        df = Frame()
        df.frame_index = f.frame_index
        df.video = dv
        if f.name:
            df.name = f.name[:150]
            df.subdir = f.subdir.replace('/',' ')
        df.save()
        if f.name:
            for l in f.subdir.split('/'):
                if l != dv.name and l.strip():
                    fl = FrameLabel()
                    fl.frame = df
                    fl.label = l
                    fl.video = dv
                    fl.source = "directory_name"
                    fl.save()
    process_video_next(video_id,start.operation)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    return 0


@app.task(name="perform_detection_by_id")
def perform_detection(video_id):
    start = TEvent()
    start.video_id = video_id
    start.started = True
    start.operation = perform_detection.name
    start.save()
    start_time = time.time()
    detector = subprocess.Popen(['fab','detect:{}'.format(video_id)],cwd=os.path.join(os.path.abspath(__file__).split('tasks.py')[0],'../'))
    detector.wait()
    face_detector = subprocess.Popen(['fab','perform_face_detection:{}'.format(video_id)],cwd=os.path.join(os.path.abspath(__file__).split('tasks.py')[0],'../'))
    face_detector.wait()
    process_video_next(video_id,start.operation)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    return 0


def perform_face_indexing(video_id):
    dv = Video.objects.get(id=video_id)
    video = entity.WVideo(dv, settings.MEDIA_ROOT)
    frames = Frame.objects.all().filter(video=dv)
    wframes = [entity.WFrame(video=video, frame_index=df.frame_index, primary_key=df.pk) for df in frames]
    input_paths = {f.local_path():f.primary_key for f in wframes}
    faces_dir = '{}/{}/detections'.format(settings.MEDIA_ROOT,video_id)
    indexes_dir = '{}/{}/indexes'.format(settings.MEDIA_ROOT,video_id)
    aligned_paths = facerecognition.align(input_paths.keys(),faces_dir)
    logging.info(len(aligned_paths))
    faces = []
    faces_to_pk = {}
    count = 0
    for path,v in aligned_paths.iteritems():
        for face_path,bb in v:
            d = Detection()
            d.video = dv
            d.confidence = 100.0
            d.frame_id = input_paths[path]
            d.object_name = "mtcnn_face"
            top, left, bottom, right = bb[0], bb[1], bb[2], bb[3]
            d.y = top
            d.x = left
            d.w = right-left
            d.h = bottom-top
            d.save()
            os.rename(face_path,'{}/{}.jpg'.format(faces_dir,d.pk))
            faces.append('{}/{}.jpg'.format(faces_dir,d.pk))
            faces_to_pk['{}/{}.jpg'.format(faces_dir,d.pk)] = d.pk
            count += 1
    dv.detections = dv.detections + count
    dv.save()
    path_count, emb_array, entries = facerecognition.represent(faces,faces_to_pk,indexes_dir)
    i = IndexEntries()
    i.video = dv
    i.count = len(entries)
    i.contains_frames = False
    i.contains_detections = True
    i.detection_name = "Face"
    i.algorithm = 'facenet'
    i.save()
