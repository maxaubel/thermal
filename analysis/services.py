import datetime
import os
import uuid

import cv2
from flask import current_app
import numpy as np
from PIL import Image, ImageFilter, ImageStat, ImageOps

from admin.services import get_group_document
from picture.services import (build_picture_path,
                              build_picture_name)
from thermal.appmodule import celery
from thermal.services import save_generic, search_generic
from thermal.utils import (get_document_with_exception,
                           item_exists,
                           log_asynchronous_exception)


def get_image_mean_pixel_value(filename):
    image = Image.open(filename).convert('L')
    stat = ImageStat.Stat(image)
    avg_pixel_value = stat.mean[0]
    return avg_pixel_value

def check_if_image_is_too_dark(filename, brightness_threshold):
    avg_pixel_value = get_image_mean_pixel_value(filename)
    if avg_pixel_value < brightness_threshold:
        return True
    return False


@celery.task
def edge_detect_chained(_, img_id_in, detection_threshold='all', auto_id=None, wide_id=None, tight_id=None):
    try:
        edge_detect(img_id_in, detection_threshold, auto_id, wide_id, tight_id)
    except Exception as e:
        log_asynchronous_exception(e)


@celery.task
def edge_detect_task(img_id_in, detection_threshold='all', auto_id=None, wide_id=None, tight_id=None):
    try:
        edge_detect(img_id_in, detection_threshold, auto_id, wide_id, tight_id)
    except Exception as e:
        log_asynchronous_exception(e)


def build_blurred_cv2_image(img_id_in):
    pic_dict_in = get_document_with_exception(img_id_in, 'picture')
    image_in = cv2.imread(pic_dict_in['uri'])
    gray = cv2.cvtColor(image_in, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    return blurred


def edge_detect_auto(img_id_in, pic_dict_in, auto_id):
    blurred = build_blurred_cv2_image(img_id_in)
    # apply Canny edge detection using an automatically determined threshold
    auto = auto_canny(blurred)
    auto_filename = build_picture_name(auto_id)
    auto_path_out = build_picture_path(picture_name=auto_filename, snap_id=pic_dict_in['snap_id'])
    cv2.imwrite(auto_path_out, auto)
    auto_dict_out = make_edge_picture_dict(pic_id=auto_id,
                                           pic_filename=auto_filename,
                                           pic_path=auto_path_out,
                                           snap_id=pic_dict_in['snap_id'],
                                           group_id=pic_dict_in['group_id'],
                                           source_pic_id=img_id_in,
                                           edge_detect_type='auto')
    save_generic(auto_dict_out, 'picture')

def edge_detect_with_canny_limits(img_id_in, pic_dict_in, new_id, limit_low, limit_high):
    blurred = build_blurred_cv2_image(img_id_in)
    # apply Canny edge detection using a custom threshold
    # TODO if limit_low or limit_high aren't positive ints, with high > low throw an error
    new_image = cv2.Canny(blurred, limit_low, limit_high)
    new_filename = build_picture_name(new_id)
    new_path_out = build_picture_path(picture_name=new_filename, snap_id=pic_dict_in['snap_id'])
    cv2.imwrite(new_path_out, new_image)
    new_dict_out = make_edge_picture_dict(pic_id=new_id,
                                          pic_filename=new_filename,
                                          pic_path=new_path_out,
                                          snap_id=pic_dict_in['snap_id'],
                                          group_id=pic_dict_in['group_id'],
                                          source_pic_id=img_id_in,
                                          edge_detect_type='custom:{0}-{1}'.format(limit_low, limit_high))
    save_generic(new_dict_out, 'picture')

def edge_detect(img_id_in, detection_threshold='all', auto_id=uuid.uuid4(), wide_id=uuid.uuid4(), tight_id=uuid.uuid4()):
    pic_dict_in = get_document_with_exception(img_id_in, 'picture')
    if detection_threshold in ['all', 'auto']:
        edge_detect_auto(img_id_in, pic_dict_in, auto_id)
    if detection_threshold in ['all', 'wide'] and wide_id:
        edge_detect_with_canny_limits(img_id_in, pic_dict_in, wide_id, 10, 200)
    if detection_threshold in ['all', 'tight'] and tight_id:
        edge_detect_with_canny_limits(img_id_in, pic_dict_in, tight_id, 225, 250)

def auto_canny(image, sigma=0.33):
    # compute the median of the single channel pixel intensities
    v = np.median(image)
    # apply automatic Canny edge detection using the computed median
    lower = int(max(0, (1.0 - sigma) * v))
    upper = int(min(255, (1.0 + sigma) * v))
    edged = cv2.Canny(image, lower, upper)
    # return the edged image
    return edged


def make_edge_picture_dict(pic_id=None,
                           pic_filename=None,
                           pic_path=None,
                           snap_id=None,
                           group_id=None,
                           source_pic_id=None,
                           edge_detect_type=None):
    img_dict_out = {
        '_id': str(pic_id),
        'type': 'picture',
        'source': 'analysis',
        'source_image_id': str(source_pic_id),
        'analysis_type': 'edge detect',
        'edge_detect_type': edge_detect_type,
        'group_id': group_id,
        'snap_id': snap_id,
        'filename': pic_filename,
        'uri': pic_path,
        'created': str(datetime.datetime.now())
    }
    return img_dict_out


@celery.task
def scale_image_chained(_, img_id_in, img_id_out, group_id, **kwargs):
    try:
        if 'scale_image' in kwargs and not kwargs['scale_image']:
            # allow this functionality to be suppressed by a kwarg for 'scale_image' that evaluates to false
            pass
        else:
            scale_image(img_id_in, img_id_out, group_id, **kwargs)
    except Exception as e:
        log_asynchronous_exception(e)


@celery.task
def scale_image_task(img_id_in, img_id_out, group_id, **kwargs):
    try:
        scale_image(img_id_in, img_id_out, group_id, **kwargs)
    except Exception as e:
        log_asynchronous_exception(e)


def scale_image(img_id_in, img_id_out, group_id, **kwargs):
    # only works on black and white images for now
    # that should only be a problem for images that aren't of type 'L'.  Add this test
    group_document = get_group_document(group_id)
    if 'scale_type' in kwargs:
        scale_type = kwargs['scale_type']
    else:
        if 'scale_type' in group_document:
            scale_type = group_document['scale_type']
        else:
            scale_type = 'colorize_bicubic'

    group_id = group_document['_id']
    img_dict_in = get_document_with_exception(str(img_id_in), 'picture')
    img_filename_in = img_dict_in['filename']
    img_filename_out = build_picture_name(img_id_out)
    pic_path_in = img_dict_in['uri']
    pic_path_out = build_picture_path(picture_name=img_filename_out, snap_id=img_dict_in['snap_id'])

    image_in = Image.open(pic_path_in)

    image_scaled = scale_image_subtask(scale_type, image_in)

    image_scaled = blur_image(scale_type, image_scaled)

    image_colorized = colorize_image(scale_type, group_document, image_scaled)
    image_colorized.save(pic_path_out)

    img_dict_out = {
        '_id': str(img_id_out),
        'type': 'picture',
        'source': 'analysis',
        'source_image_id': str(img_id_in),
        'analysis_type': scale_type,
        'group_id': group_id,
        'snap_id': img_dict_in['snap_id'],
        'filename': img_filename_out,
        'uri': pic_path_out,
        'created': str(datetime.datetime.now())
    }
    save_generic(img_dict_out, 'picture')

def blur_image(scale_type, image):
    # TODO: below is terribly inefficient.  After I look at PIL internals I should be able to do better
    if scale_type and 'blur' in scale_type:
        for i in range(1, 10):
            image = image.filter(ImageFilter.BLUR)
    return image

def scale_image_subtask(scale_type, image_in):
    scale_method = Image.BICUBIC
    if scale_type and 'bilinear' in scale_type:
        scale_method = Image.BILINEAR
    if scale_type and 'antialias' in scale_type:
        scale_method = Image.ANTIALIAS
    width = current_app.config['STILL_IMAGE_WIDTH']
    height = current_app.config['STILL_IMAGE_HEIGHT']
    image_scaled = image_in.resize((width, height), scale_method)
    return image_scaled

def colorize_image(scale_type, group_document, image_in):
    if scale_type and 'colorize' in scale_type:
        (colorize_range_low, colorize_range_high) = ('#000080', '#FFD700')
        if 'colorize_range_low' in group_document and 'colorize_range_high' in group_document:
            colorize_range_low = group_document['colorize_range_low']
            colorize_range_high = group_document['colorize_range_high']
        image_colorized = ImageOps.colorize(image_in, colorize_range_low, colorize_range_high)
        return image_colorized
    else:
        return image_in


@celery.task
def distort_image_shepards_chained(_, img_id_in, img_id_out):
    '''
    For now assume no distortion_set_id for chained distortions.
    If we need a distortion set we'll get whatever has been associated with the group
    '''
    try:
        distort_image_shepards(image_id_in=img_id_in, image_id_out=img_id_out, distortion_set_id=None)
    except Exception as e:
        log_asynchronous_exception(e)

@celery.task
def distort_image_shepards_task(img_id_in, img_id_out, distortion_set_id):
    try:
        distort_image_shepards(image_id_in=img_id_in, image_id_out=img_id_out, distortion_set_id=distortion_set_id)
    except Exception as e:
        log_asynchronous_exception(e)


def build_distortion_pair_strings(distortion_set_id):
    distortion_pairs = search_generic(document_type='distortion_pair',
                                      args_dict={'distortion_set_id': distortion_set_id})
    return_array = []
    for distortion_pair in distortion_pairs:
        build_string = '{0},{1},{2},{3}'.format(distortion_pair['start_x'],
                                                distortion_pair['start_y'],
                                                distortion_pair['end_x'],
                                                distortion_pair['end_y'])
        return_array.append(build_string)
    return return_array
    

def build_command_string(distortion_set_id, pic_path_in, pic_path_out):
    distortion_pair_strings = build_distortion_pair_strings(distortion_set_id)
    distortion_pair_string = ' '.join(distortion_pair_strings)
    # TODO remove the below hardcoded string as soon as we're ready to proceed with the ui for distortion sets
    distortion_pair_string = '300,110 350,140  600,310 650,340'
    print 'distortion pair string is '+distortion_pair_string

    command_string = "convert {0} -distort Shepards '{1}' {2}".format(pic_path_in,
                                                                      distortion_pair_string,
                                                                      pic_path_out)
    print 'command is '+command_string
    return command_string


def distort_image_shepards(image_id_in=None, image_id_out=None, distortion_set_id=None):
    '''
    Distorts an image using all the distortion pairs in a named distortion set
    It is necessary to call ImageMagick via command line to make this happen, no bindings in Pillow for this functionality :(
    Uses the Shepards algorithm for distortion
    '''
    img_dict_in = get_document_with_exception(image_id_in, 'picture')
    group_id = img_dict_in['group_id']
    img_filename_out = build_picture_name(image_id_out)
    pic_path_in = img_dict_in['uri']
    pic_path_out = build_picture_path(picture_name=img_filename_out, snap_id=img_dict_in['snap_id'])


    command_string = build_command_string(distortion_set_id, pic_path_in, pic_path_out)
    os.system(command_string)

    img_dict_out = {
        '_id': str(image_id_out),
        'type': 'picture',
        'source': 'analysis',
        'source_image_id': str(image_id_in),
        'analysis_type': 'distort',
        'group_id': group_id,
        'snap_id': img_dict_in['snap_id'],
        'filename': img_filename_out,
        'uri': pic_path_out,
        'created': str(datetime.datetime.now())
    }
    save_generic(img_dict_out, 'picture')
