# coding=utf-8
# single gpu only

import sys,os,argparse
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' # so here won't have poll allocator info

# remove all the annoying warnings from tf v1.10 to v1.13
import logging
logging.getLogger('tensorflow').disabled = True

from tqdm import tqdm
import numpy as np
import tensorflow as tf

import cv2

from models import get_model
from nn import resizeImage

import math, time, json, random, operator
import _pickle as pickle
import pycocotools.mask as cocomask


from utils import Dataset, Summary, get_op_tensor_name

from class_ids import targetClass2id_new_nopo
targetClass2id = targetClass2id_new_nopo

targetid2class = {targetClass2id[one]:one for one in targetClass2id}

def get_args():
	global targetClass2id, targetid2class
	parser = argparse.ArgumentParser()

	parser.add_argument("--video_dir", default=None)
	parser.add_argument("--video_lst_file", default=None, help="video_file_path = os.path.join(video_dir, $line)")

	parser.add_argument("--out_dir", default=None, help="out_dir/$basename/%%d.json, start from 0 index")

	parser.add_argument("--frame_gap", default=8, type=int)

	parser.add_argument("--threshold_conf",default=0.0001,type=float)

	parser.add_argument("--is_load_from_pb", action="store_true", help="load from a frozen graph")

	# ------ for box feature extraction
	parser.add_argument("--get_box_feat", action="store_true",help="this will generate (num_box, 256, 7, 7) tensor for each frame")
	parser.add_argument("--box_feat_path", default=None,help="output will be out_dir/$basename/%%d.npy, start from 0 index")

	# ---- gpu params
	parser.add_argument("--gpu", default=1, type=int, help="number of gpu")
	parser.add_argument("--gpuid_start", default=0, type=int, help="start of gpu id")
	parser.add_argument('--im_batch_size', type=int, default=1)
	parser.add_argument("--use_all_mem", action="store_true")

	parser.add_argument("--version", type=int, default=4, help="model version")

	# --- for internal visualization
	parser.add_argument("--visualize", action="store_true")
	parser.add_argument("--vis_path", default=None)
	parser.add_argument("--vis_thres", default=0.7, type=float)
	

	# ----------- model params
	parser.add_argument("--num_class", type=int, default=15, help="num catagory + 1 background")

	parser.add_argument("--model_path", default="/app/object_detection_model")

	parser.add_argument("--rpn_batch_size", type=int, default=256, help="num roi per image for RPN  training")
	parser.add_argument("--frcnn_batch_size", type=int, default=512, help="num roi per image for fastRCNN training")
	
	parser.add_argument("--rpn_test_post_nms_topk", type=int, default=1000 ,help="test post nms, input to fast rcnn")

	parser.add_argument("--max_size", type=int, default=1920, help="num roi per image for RPN and fastRCNN training")
	parser.add_argument("--short_edge_size", type=int, default=1080, help="num roi per image for RPN and fastRCNN training")

	# ---- tempory: for activity detection model
	parser.add_argument("--actasobj", action="store_true")
	parser.add_argument("--actmodel_path", default="/app/activity_detection_model")

	parser.add_argument("--resnet152",action="store_true",help="")
	parser.add_argument("--resnet50",action="store_true",help="")
	parser.add_argument("--resnet34",action="store_true",help="")
	parser.add_argument("--resnet18",action="store_true",help="")
	parser.add_argument("--use_se",action="store_true",help="use squeeze and excitation in backbone")
	parser.add_argument("--use_frcnn_class_agnostic", action="store_true", help="use class agnostic fc head")
	parser.add_argument("--use_att_frcnn_head", action="store_true",help="use attention to sum [K, 7, 7, C] feature into [K, C]")


	# --------------- exp junk
	parser.add_argument("--use_dilations", action="store_true", help="use dilations=2 in res5")
	parser.add_argument("--use_deformable", action="store_true", help="use deformable conv")
	parser.add_argument("--add_act",action="store_true", help="add activitiy model")
	parser.add_argument("--finer_resolution", action="store_true", help="fpn use finer resolution conv")
	parser.add_argument("--fix_fpn_model", action="store_true", help="for finetuneing a fpn model, whether to fix the lateral and poshoc weights")
	parser.add_argument("--is_cascade_rcnn", action="store_true", help="cascade rcnn on top of fpn")
	parser.add_argument("--add_relation_nn", action="store_true", help="add relation network feature")

	parser.add_argument("--test_frame_extraction", action="store_true")
	parser.add_argument("--use_my_naming", action="store_true")

	args = parser.parse_args()
	
	assert args.gpu == args.im_batch_size # one gpu one image
	assert args.gpu == 1, "Currently only support single-gpu inference"

	if args.is_load_from_pb:
		args.load_from = args.model_path

	args.controller = "/cpu:0" # parameter server

	targetid2class = targetid2class
	targetClass2id = targetClass2id

	if args.actasobj:
		from class_ids import targetAct2id
		targetClass2id = targetAct2id
		targetid2class = {targetAct2id[one]:one for one in targetAct2id}

	assert len(targetClass2id) == args.num_class, (len(targetClass2id), args.num_class)

	assert args.version in [2, 3, 4, 5, 6], "Currently we only have version 2-6 model"
	if args.version == 2:
		pass
	elif args.version == 3:
		args.use_dilations = True
	elif args.version == 4:
		args.use_frcnn_class_agnostic = True
		args.use_dilations = True
	elif args.version == 5:
		args.use_frcnn_class_agnostic = True
		args.use_dilations = True
	elif args.version == 6:
		args.use_frcnn_class_agnostic = True
		args.use_se = True

	# ---------------more defautls
	args.is_pack_model = False
	args.diva_class3 = True
	args.diva_class = False
	args.diva_class2 = False
	args.use_small_object_head = False
	args.use_so_score_thres = False
	args.use_so_association = False
	args.use_gn = False
	args.so_person_topk = 10
	args.use_conv_frcnn_head = False
	args.use_cpu_nms = False
	args.use_bg_score = False
	args.freeze_rpn = True
	args.freeze_fastrcnn = True
	args.freeze = 2
	args.small_objects = ["Prop", "Push_Pulled_Object", "Prop_plus_Push_Pulled_Object", "Bike"]
	args.no_obj_detect = False
	args.add_mask = False
	args.is_fpn = True
	#args.new_tensorpack_model = True
	args.mrcnn_head_dim = 256
	args.is_train = False

	args.rpn_min_size = 0
	args.rpn_proposal_nms_thres = 0.7
	args.anchor_strides = (4, 8, 16, 32, 64)
		

	args.fpn_resolution_requirement = float(args.anchor_strides[3]) # [3] is 32, since we build FPN with r2,3,4,5?
		
	args.max_size = np.ceil(args.max_size / args.fpn_resolution_requirement) * args.fpn_resolution_requirement

	args.fpn_num_channel = 256

	args.fpn_frcnn_fc_head_dim = 1024

	# ---- all the mask rcnn config

	args.resnet_num_block = [3, 4, 23, 3] # resnet 101
	args.use_basic_block = False # for resnet-34 and resnet-18
	if args.resnet152:
		args.resnet_num_block = [3, 8, 36, 3]
	if args.resnet50:
		args.resnet_num_block = [3, 4, 6, 3]
	if args.resnet34:
		args.resnet_num_block = [3, 4, 6, 3]
		args.use_basic_block = True
	if args.resnet18:
		args.resnet_num_block = [2, 2, 2, 2]
		args.use_basic_block = True
	
	args.anchor_stride = 16 # has to be 16 to match the image feature total stride
	args.anchor_sizes = (32,64,128,256,512)

	args.anchor_ratios = (0.5, 1, 2)
	

	args.num_anchors = len(args.anchor_sizes) * len(args.anchor_ratios)
	# iou thres to determine anchor label
	#args.positive_anchor_thres = 0.7
	#args.negative_anchor_thres = 0.3

	# when getting region proposal, avoid getting too large boxes
	args.bbox_decode_clip = np.log(args.max_size / 16.0)


	# fastrcnn
	args.fastrcnn_batch_per_im = args.frcnn_batch_size
	args.fastrcnn_bbox_reg_weights = np.array([10, 10, 5, 5], dtype='float32')
	
	args.fastrcnn_fg_thres = 0.5 # iou thres
	#args.fastrcnn_fg_ratio = 0.25 # 1:3 -> pos:neg

	# testing
	args.rpn_test_pre_nms_topk = 6000

	args.fastrcnn_nms_iou_thres = 0.5

	args.result_score_thres = args.threshold_conf
	args.result_per_im = 100 

	return args

def initialize(config,sess):
	tf.global_variables_initializer().run()
	allvars = tf.global_variables()
	allvars = [var for var in allvars if "global_step" not in var.name]
	restore_vars = allvars
	opts = ["Adam","beta1_power","beta2_power","Adam_1","Adadelta_1","Adadelta","Momentum"]
	restore_vars = [var for var in restore_vars if var.name.split(":")[0].split("/")[-1] not in opts]

	saver = tf.train.Saver(restore_vars, max_to_keep=5)

	load_from = config.model_path	
	ckpt = tf.train.get_checkpoint_state(load_from)
	if ckpt and ckpt.model_checkpoint_path:
		loadpath = ckpt.model_checkpoint_path					
		saver.restore(sess, loadpath)
	else:
		raise Exception("Model not exists")

# check argument
def check_args(args):
	assert args.video_dir is not None
	assert args.video_lst_file is not None
	assert args.frame_gap >=1
	if args.get_box_feat:
		assert args.box_feat_path is not None
		if not os.path.exists(args.box_feat_path):
			os.makedirs(args.box_feat_path)
	#print "cv2 version %s"%(cv2.__version__)


if __name__ == "__main__":
	args = get_args()

	check_args(args)

	videolst = [os.path.join(args.video_dir, one.strip()) for one in open(args.video_lst_file).readlines()]

	if not os.path.exists(args.out_dir):
		os.makedirs(args.out_dir)

	if args.visualize:
		from viz import draw_boxes
		vis_path = args.vis_path
		if not os.path.exists(vis_path):
			os.makedirs(vis_path)

	# 1. load the object detection model
	model = get_model(args,args.gpuid_start, controller=args.controller)

	tfconfig = tf.ConfigProto(allow_soft_placement=True)
	if not args.use_all_mem:
		tfconfig.gpu_options.allow_growth = True
	tfconfig.gpu_options.visible_device_list = "%s"%(",".join(["%s"%i for i in range(args.gpuid_start, args.gpuid_start+args.gpu)]))

	with tf.Session(config=tfconfig) as sess:

		if not args.is_load_from_pb:
			initialize(config=args, sess=sess)

		for videofile in tqdm(videolst, ascii=True):
			# 2. read the video file
			try:
				vcap = cv2.VideoCapture(videofile)
				if not vcap.isOpened():
					raise Exception("cannot open %s"%videofile)
			except Exception as e:
				raise e

			#videoname = os.path.splitext(os.path.basename(videofile))[0]
			videoname = os.path.basename(videofile)
			video_out_path = os.path.join(args.out_dir, videoname)
			if not os.path.exists(video_out_path):
				os.makedirs(video_out_path)

			# for box feature
			if args.get_box_feat:
				feat_out_path = os.path.join(args.box_feat_path, videoname)
				if not os.path.exists(feat_out_path):
					os.makedirs(feat_out_path)
					
			# opencv 2
			if cv2.__version__.split(".")[0] == "2":
				frame_count = vcap.get(cv2.cv.CV_CAP_PROP_FRAME_COUNT)
			else:
				# opencv 3/4
				frame_count = vcap.get(cv2.CAP_PROP_FRAME_COUNT)

			# 3. read frame one by one
			cur_frame=0
			vis_count=0
			frame_stack = []
			while cur_frame < frame_count:
				suc, frame = vcap.read()
				if not suc:
					cur_frame+=1
					tqdm.write("warning, %s frame of %s failed"%(cur_frame,videoname))
					continue

				# skip some frame if frame_gap >1
				if cur_frame % args.frame_gap != 0:
					cur_frame+=1
					continue

				# 4. run detection on the frame stack if there is enough

				im = frame.astype("float32")

				if args.test_frame_extraction:
					frame_file = os.path.join(video_out_path, "%d.jpg"%cur_frame)
					cv2.imwrite(frame_file, im)
					cur_frame+=1
					continue

				resized_image = resizeImage(im, args.short_edge_size, args.max_size)

				scale = (resized_image.shape[0]*1.0/im.shape[0] + resized_image.shape[1]*1.0/im.shape[1])/2.0

				feed_dict = model.get_feed_dict_forward(resized_image)

				if args.get_box_feat:
					sess_input = [model.final_boxes, model.final_labels, model.final_probs, model.fpn_box_feat]

					final_boxes, final_labels, final_probs, box_feats = sess.run(sess_input,feed_dict=feed_dict)
					assert len(box_feats) == len(final_boxes)
					# save the box feature first

					featfile = os.path.join(feat_out_path, "%d.npy"%(cur_frame))
					np.save(featfile, box_feats)
				else:
					sess_input = [model.final_boxes, model.final_labels, model.final_probs]

					final_boxes, final_labels, final_probs = sess.run(sess_input, feed_dict=feed_dict)
				#print "sess run done"
				# scale back the box to original image size
				final_boxes = final_boxes / scale

				# save as json
				pred = []

				for j,(box, prob, label) in enumerate(zip(final_boxes,final_probs,final_labels)):
					box[2] -= box[0]
					box[3] -= box[1] # produce x,y,w,h output

					cat_id = label
					cat_name = targetid2class[cat_id]

					# encode mask
					rle = None

					res = {
						"category_id":int(cat_id),
						"cat_name":cat_name, #[0-80]
						"score":float(round(prob,7)),
						"bbox": list(map(lambda x:float(round(x,2)),box)),
						"segmentation":rle,
					}

					pred.append(res)

				#predfile = os.path.join(args.out_dir, "%s_F_%08d.json"%(videoname, cur_frame))
				if args.use_my_naming:
					predfile = os.path.join(video_out_path, "%s_F_%08d.json"%(os.path.splitext(videoname)[0], cur_frame))
				else:
					predfile = os.path.join(video_out_path, "%d.json"%(cur_frame))
				with open(predfile,"w") as f:
					json.dump(pred, f)

				# for visualization
				if args.visualize:
					good_ids = [i for i in range(len(final_boxes)) if final_probs[i] >= args.vis_thres]
					final_boxes,final_labels,final_probs = final_boxes[good_ids],final_labels[good_ids],final_probs[good_ids]
					vis_boxes = np.asarray([[box[0], box[1], box[2]+box[0], box[3]+box[1]] for box in final_boxes])
					vis_labels = ["%s_%.2f"%(targetid2class[cat_id],prob) for cat_id,prob in zip(final_labels,final_probs)]
					newim = draw_boxes(im,vis_boxes,vis_labels, color=(255,0,0),font_scale=0.5,thickness=2)

					vis_file = os.path.join(vis_path,"%s_F_%08d.jpg"%(videoname,vis_count))
					cv2.imwrite(vis_file, newim)
					vis_count+=1

				cur_frame+=1

			if args.test_frame_extraction:
				tqdm.write("video %s got %s frames, opencv said frame count is %s" % (videoname, cur_frame, frame_count))

