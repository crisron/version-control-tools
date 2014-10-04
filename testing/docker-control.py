#!/usr/bin/env python
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

# This script is used to manage Docker containers in the context of running
# Mercurial tests.

import docker
import json
import os
import socket
import sys
import time
import urlparse

HERE = os.path.abspath(os.path.dirname(__file__))
DOCKER_DIR = os.path.join(HERE, 'docker')

# Unbuffer stdout.
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 0)

def wait_for_socket(host, port, timeout=60):
    """Wait for a TCP socket to accept connections."""

    start = time.time()

    while True:
        try:
            socket.create_connection((host, port), timeout=1)
            return
        except socket.error:
            pass

        if time.time() - start > timeout:
            raise Exception('Timeout reached waiting for socket')

        time.sleep(1)

class Docker(object):
    def __init__(self, state_path, url):
        self._ddir = DOCKER_DIR
        self._state_path = state_path
        self.state = {
            'images': {},
            'containers': {},
        }

        if os.path.exists(state_path):
            with open(state_path, 'rb') as fh:
                self.state = json.load(fh)

        self.client = docker.Client(base_url=url)

    def ensure_built(self, name):
        """Ensure a Docker image from a builder directory is built."""
        p = os.path.join(self._ddir, 'builder-%s' % name)
        if not os.path.isdir(p):
            raise Exception('Unknown Docker builder name: %s' % name)

        image = self.state['images'].get(name)
        if image:
            return image

        # TODO create a lock to avoid race conditions.

        # The API here is wonky, possibly due to buggy behavior in
        # docker.client always setting stream=True if version > 1.8.
        # We assume this is a bug that will change behavior later and work
        # around it by ensuring consisten behavior.
        print('Building Docker image %s' % name)
        for stream in self.client.build(path=p, stream=True):
            s = json.loads(stream)
            if 'stream' not in s:
                continue

            s = s['stream']
            #sys.stdout.write(s)
            if s.startswith('Successfully built '):
                image = s[len('Successfully built '):]
                # There is likely a trailing newline.
                image = image.rstrip()
                break

        self.state['images'][name] = image
        self.save_state()

        return image

    def build_bmo(self):
        """Ensure the images for a BMO service are built.

        bmoweb's entrypoint does a lot of setup on first run. This takes many
        seconds to perform and this cost is unacceptable for efficient test
        running. So, when we build the BMO images, we create throwaway
        containers and commit the results to a new image. This allows us to
        spin up multiple bmoweb containers very quickly.
        """
        images = self.state['images']
        db_image = self.ensure_built('bmodb-volatile')
        web_image = self.ensure_built('bmoweb')

        if 'bmoweb-bootstrapped' in images:
            return images['bmodb-bootstrapped'], images['bmoweb-bootstrapped']

        db_id = self.client.create_container(db_image,
                environment={'MYSQL_ROOT_PASSWORD': 'password'})['Id']

        web_id = self.client.create_container(web_image)['Id']

        self.client.start(db_id)
        db_state = self.client.inspect_container(db_id)

        self.client.start(web_id,
                links=[(db_state['Name'], 'bmodb')],
                port_bindings={80: None})
        web_state = self.client.inspect_container(web_id)

        hostname = urlparse.urlparse(self.client.base_url).hostname
        http_port = int(web_state['NetworkSettings']['Ports']['80/tcp'][0]['HostPort'])
        print('waiting for bmoweb to bootstrap')
        wait_for_socket(hostname, http_port)

        db_bootstrap = self.client.commit(db_id)['Id']
        web_bootstrap = self.client.commit(web_id)['Id']
        self.state['images']['bmodb-bootstrapped'] = db_bootstrap
        self.state['images']['bmoweb-bootstrapped'] = web_bootstrap
        self.save_state()

        self.client.kill(web_id)
        self.client.kill(db_id)
        db_state = self.client.inspect_container(db_id)
        web_state = self.client.inspect_container(web_id)

        if web_state['State']['Running']:
            self.client.stop(web_id)
        if db_state['State']['Running']:
            self.client.stop(db_id)

        self.client.remove_container(web_id)
        self.client.remove_container(db_id)

        return db_bootstrap, web_bootstrap

    def start_bmo(self, cluster, hostname=None, http_port=80):
        db_image, web_image = self.build_bmo()

        containers = self.state['containers'].setdefault(cluster, [])

        docker_hostname = urlparse.urlparse(self.client.base_url).hostname

        # Default hostname is the hostname running Docker.
        if not hostname:
            hostname = docker_hostname
        url = 'http://%s:%s/' % (hostname, http_port)

        db_id = self.client.create_container(db_image,
                environment={'MYSQL_ROOT_PASSWORD': 'password'})['Id']
        containers.append(db_id)
        web_id = self.client.create_container(web_image,
                environment={'BMO_URL': url})['Id']
        containers.append(web_id)
        self.save_state()

        self.client.start(db_id)
        db_state = self.client.inspect_container(db_id)
        self.client.start(web_id,
                links=[(db_state['Name'], 'bmodb')],
                port_bindings={80: http_port})
        web_state = self.client.inspect_container(web_id)

        print('waiting for Bugzilla HTTP server to start...')
        wait_for_socket(docker_hostname, http_port)

    def stop_bmo(self, cluster):
        for container in self.state['containers'].get(cluster, []):
            self.client.kill(container)
            self.client.stop(container)
            info = self.client.inspect_container(container)
            self.client.remove_container(container)

            image = info['Image']
            if image not in self.state['images'].values():
                self.client.remove_image(info['Image'])

        try:
            del self.state['containers'][cluster]
            self.save_state()
        except KeyError:
            pass

    def save_state(self):
        with open(self._state_path, 'wb') as fh:
            json.dump(self.state, fh)

def main(args):
    if 'DOCKER_STATE_FILE' not in os.environ:
        print('DOCKER_STATE_FILE must be defined')
        return 1

    docker_url = os.environ.get('DOCKER_HOST', None)

    d = Docker(os.environ['DOCKER_STATE_FILE'], docker_url)

    action = args[0]

    if action == 'build-bmo':
        d.build_bmo()
    elif action == 'start-bmo':
        cluster, http_port = args[1:]
        d.start_bmo(cluster=cluster, hostname=None, http_port=http_port)
    elif action == 'stop-bmo':
        d.stop_bmo(cluster=args[1])

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
