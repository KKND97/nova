# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Handles all API requests relating to instances (guest vms).
"""

import datetime
import logging
import time
import functools

from nova import db
from nova import exception
from nova import flags
from nova import quota
from nova import rpc
from nova import utils
from nova.compute import instance_types
from nova.db import base

FLAGS = flags.FLAGS


def checks_instance_lock(function):
    """
    decorator used for preventing action against locked instances
    unless, of course, you happen to be admin

    """

    @functools.wraps(function)
    def decorated_function(*args, **kwargs):

        # assume worst case (have to declare so they are in scope)
        admin = False
        locked = True

        # grab args to function
        try:
            if 'context' in kwargs:
                context = kwargs['context']
            else:
                context = args[1]
            if 'instance_id' in kwargs:
                instance_id = kwargs['instance_id']
            else:
                instance_id = args[2]
            locked = ComputeAPI().get_lock(context, instance_id)
            admin = context.is_admin
        except:
            raise Exception(_("check_instance_lock argument error |%s|, |%s|"),
                                                                        args,
                                                                        kwargs)
        # if admin or unlocked call function, otherwise 405
        if admin or not locked:
            return function(*args, **kwargs)
        raise Exception(_("Instance is locked, cannot execute |%s|"), function)

    return decorated_function


def generate_default_hostname(internal_id):
    """Default function to generate a hostname given an instance reference."""
    return str(internal_id)


class ComputeAPI(base.Base):
    """API for interacting with the compute manager."""

    def __init__(self, network_manager=None, image_service=None, **kwargs):
        if not network_manager:
            network_manager = utils.import_object(FLAGS.network_manager)
        self.network_manager = network_manager
        if not image_service:
            image_service = utils.import_object(FLAGS.image_service)
        self.image_service = image_service
        super(ComputeAPI, self).__init__(**kwargs)

    def get_network_topic(self, context, instance_id):
        try:
            instance = self.db.instance_get_by_internal_id(context,
                                                           instance_id)
        except exception.NotFound as e:
            logging.warning("Instance %d was not found in get_network_topic",
                            instance_id)
            raise e

        host = instance['host']
        if not host:
            raise exception.Error("Instance %d has no host" % instance_id)
        topic = self.db.queue_get_for(context, FLAGS.compute_topic, host)
        return rpc.call(context,
                        topic,
                        {"method": "get_network_topic", "args": {'fake': 1}})

    def create_instances(self, context, instance_type, image_id, min_count=1,
                         max_count=1, kernel_id=None, ramdisk_id=None,
                         display_name='', description='', key_name=None,
                         key_data=None, security_group='default',
                         user_data=None,
                         generate_hostname=generate_default_hostname):
        """Create the number of instances requested if quote and
        other arguments check out ok."""

        num_instances = quota.allowed_instances(context, max_count,
                                                instance_type)
        if num_instances < min_count:
            logging.warn("Quota exceeeded for %s, tried to run %s instances",
                         context.project_id, min_count)
            raise quota.QuotaError("Instance quota exceeded. You can only "
                                   "run %s more instances of this type." %
                                   num_instances, "InstanceLimitExceeded")

        is_vpn = image_id == FLAGS.vpn_image_id
        if not is_vpn:
            image = self.image_service.show(context, image_id)
            if kernel_id is None:
                kernel_id = image.get('kernelId', None)
            if ramdisk_id is None:
                ramdisk_id = image.get('ramdiskId', None)
            #No kernel and ramdisk for raw images
            if kernel_id == str(FLAGS.null_kernel):
                kernel_id = None
                ramdisk_id = None
                logging.debug("Creating a raw instance")
            # Make sure we have access to kernel and ramdisk (if not raw)
            if kernel_id:
                self.image_service.show(context, kernel_id)
            if ramdisk_id:
                self.image_service.show(context, ramdisk_id)

        if security_group is None:
            security_group = ['default']
        if not type(security_group) is list:
            security_group = [security_group]

        security_groups = []
        self.ensure_default_security_group(context)
        for security_group_name in security_group:
            group = db.security_group_get_by_name(context,
                                                  context.project_id,
                                                  security_group_name)
            security_groups.append(group['id'])

        if key_data is None and key_name:
            key_pair = db.key_pair_get(context, context.user_id, key_name)
            key_data = key_pair['public_key']

        type_data = instance_types.INSTANCE_TYPES[instance_type]
        base_options = {
            'reservation_id': utils.generate_uid('r'),
            'image_id': image_id,
            'kernel_id': kernel_id or '',
            'ramdisk_id': ramdisk_id or '',
            'state_description': 'scheduling',
            'user_id': context.user_id,
            'project_id': context.project_id,
            'launch_time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'instance_type': instance_type,
            'memory_mb': type_data['memory_mb'],
            'vcpus': type_data['vcpus'],
            'local_gb': type_data['local_gb'],
            'display_name': display_name,
            'display_description': description,
            'user_data': user_data or '',
            'key_name': key_name,
            'key_data': key_data,
            'locked': False}

        elevated = context.elevated()
        instances = []
        logging.debug(_("Going to run %s instances..."), num_instances)
        for num in range(num_instances):
            instance = dict(mac_address=utils.generate_mac(),
                            launch_index=num,
                            **base_options)
            instance = self.db.instance_create(context, instance)
            instance_id = instance['id']
            internal_id = instance['internal_id']

            elevated = context.elevated()
            if not security_groups:
                security_groups = []
            for security_group_id in security_groups:
                self.db.instance_add_security_group(elevated,
                                                    instance_id,
                                                    security_group_id)

            # Set sane defaults if not specified
            updates = dict(hostname=generate_hostname(internal_id))
            if 'display_name' not in instance:
                updates['display_name'] = "Server %s" % internal_id

            instance = self.update_instance(context, instance_id, **updates)
            instances.append(instance)

            logging.debug(_("Casting to scheduler for %s/%s's instance %s"),
                          context.project_id, context.user_id, instance_id)
            rpc.cast(context,
                     FLAGS.scheduler_topic,
                     {"method": "run_instance",
                      "args": {"topic": FLAGS.compute_topic,
                               "instance_id": instance_id}})

        return instances

    def ensure_default_security_group(self, context):
        """ Create security group for the security context if it
        does not already exist

        :param context: the security context

        """
        try:
            db.security_group_get_by_name(context, context.project_id,
                                          'default')
        except exception.NotFound:
            values = {'name': 'default',
                      'description': 'default',
                      'user_id': context.user_id,
                      'project_id': context.project_id}
            db.security_group_create(context, values)

    @checks_instance_lock
    def update_instance(self, context, instance_id, **kwargs):
        """Updates the instance in the datastore.

        :param context: The security context
        :param instance_id: ID of the instance to update
        :param kwargs: All additional keyword args are treated
                       as data fields of the instance to be
                       updated

        :retval None

        """
        return self.db.instance_update(context, instance_id, kwargs)

    @checks_instance_lock
    def delete_instance(self, context, instance_id):
        logging.debug("Going to try and terminate %d" % instance_id)
        try:
            instance = self.db.instance_get_by_internal_id(context,
                                                           instance_id)
        except exception.NotFound as e:
            logging.warning(_("Instance %d was not found during terminate"),
                            instance_id)
            raise e

        if (instance['state_description'] == 'terminating'):
            logging.warning(_("Instance %d is already being terminated"),
                            instance_id)
            return

        self.update_instance(context,
                             instance['id'],
                             state_description='terminating',
                             state=0,
                             terminated_at=datetime.datetime.utcnow())

        host = instance['host']
        if host:
            rpc.cast(context,
                     self.db.queue_get_for(context, FLAGS.compute_topic, host),
                     {"method": "terminate_instance",
                      "args": {"instance_id": instance['id']}})
        else:
            self.db.instance_destroy(context, instance['id'])

    def get_instances(self, context, project_id=None):
        """Get all instances, possibly filtered by project ID or
        user ID. If there is no filter and the context is an admin,
        it will retreive all instances in the system."""
        if project_id or not context.is_admin:
            if not context.project:
                return self.db.instance_get_all_by_user(context,
                                                        context.user_id)
            if project_id is None:
                project_id = context.project_id
            return self.db.instance_get_all_by_project(context, project_id)
        return self.db.instance_get_all(context)

    def get_instance(self, context, instance_id):
        return self.db.instance_get_by_internal_id(context, instance_id)

    @checks_instance_lock
    def reboot(self, context, instance_id):
        """Reboot the given instance."""
        instance = self.db.instance_get_by_internal_id(context, instance_id)
        host = instance['host']
        rpc.cast(context,
                 self.db.queue_get_for(context, FLAGS.compute_topic, host),
                 {"method": "reboot_instance",
                  "args": {"instance_id": instance['id']}})

    @checks_instance_lock
    def pause(self, context, instance_id):
        """Pause the given instance."""
        instance = self.db.instance_get_by_internal_id(context, instance_id)
        host = instance['host']
        rpc.cast(context,
                 self.db.queue_get_for(context, FLAGS.compute_topic, host),
                 {"method": "pause_instance",
                  "args": {"instance_id": instance['id']}})

    @checks_instance_lock
    def unpause(self, context, instance_id):
        """Unpause the given instance."""
        instance = self.db.instance_get_by_internal_id(context, instance_id)
        host = instance['host']
        rpc.cast(context,
                 self.db.queue_get_for(context, FLAGS.compute_topic, host),
                 {"method": "unpause_instance",
                  "args": {"instance_id": instance['id']}})

    @checks_instance_lock
    def suspend(self, context, instance_id):
        """suspend the instance with instance_id"""
        instance = self.db.instance_get_by_internal_id(context, instance_id)
        host = instance['host']
        rpc.cast(context,
                 self.db.queue_get_for(context, FLAGS.compute_topic, host),
                 {"method": "suspend_instance",
                  "args": {"instance_id": instance['id']}})

    @checks_instance_lock
    def resume(self, context, instance_id):
        """resume the instance with instance_id"""
        instance = self.db.instance_get_by_internal_id(context, instance_id)
        host = instance['host']
        rpc.cast(context,
                 self.db.queue_get_for(context, FLAGS.compute_topic, host),
                 {"method": "resume_instance",
                  "args": {"instance_id": instance['id']}})

    @checks_instance_lock
    def rescue(self, context, instance_id):
        """Rescue the given instance."""
        instance = self.db.instance_get_by_internal_id(context, instance_id)
        host = instance['host']
        rpc.cast(context,
                 self.db.queue_get_for(context, FLAGS.compute_topic, host),
                 {"method": "rescue_instance",
                  "args": {"instance_id": instance['id']}})

    @checks_instance_lock
    def unrescue(self, context, instance_id):
        """Unrescue the given instance."""
        instance = self.db.instance_get_by_internal_id(context, instance_id)
        host = instance['host']
        rpc.cast(context,
                 self.db.queue_get_for(context, FLAGS.compute_topic, host),
                 {"method": "unrescue_instance",
                  "args": {"instance_id": instance['id']}})

    def lock(self, context, instance_id):
        """
        lock the instance with instance_id

        """
        instance = self.get_instance(context, instance_id)
        host = instance['host']
        rpc.cast(context,
                 self.db.queue_get_for(context, FLAGS.compute_topic, host),
                 {"method": "lock_instance",
                  "args": {"instance_id": instance['id']}})

    def unlock(self, context, instance_id):
        """
        unlock the instance with instance_id

        """
        instance = self.get_instance(context, instance_id)
        host = instance['host']
        rpc.cast(context,
                 self.db.queue_get_for(context, FLAGS.compute_topic, host),
                 {"method": "unlock_instance",
                  "args": {"instance_id": instance['id']}})

    def get_lock(self, context, instance_id):
        """
        return the boolean state of (instance with instance_id)'s lock

        """
        instance = self.get_instance(context, instance_id)
        return instance['locked']
