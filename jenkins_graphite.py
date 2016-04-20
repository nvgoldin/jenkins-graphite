# Send Jenkins metrics to graphite
from graphitesend import graphitesend
from backports.functools_lru_cache import lru_cache
from collections import Counter
import time
import logging
import jenkins
import operator
import re
import xmltodict
import argparse
import os
import sys


class QueueInfo(object):
    """QueueInfo:"""

    def __init__(self):
        self.labels = {}
        self.jobs = []
        self.total = 0


def send_graphite(data, graphite_url, prefix):
    """
    send_graphite

    :param data:
    :param graphite_url:
    :param prefix:
    """
    if not data:
        logging.debug('got empty data, not sending, prefix: %s', prefix)
        return
    graphitesend.init(graphite_server=graphite_url, group=prefix)
    if isinstance(data, list):
        logging.debug('group prefix: %s send list: %s', prefix, data)
        graphitesend.send_list(data)
    elif isinstance(data, dict):
        for dictonary in _flat_and_send(data, expanded_key='', sep='.'):
            logging.debug('group prefix: %s sending dict: %s', prefix,
                          dictonary)
            graphitesend.send_dict(dictonary)
    else:
        logging.debug('unrecognized datatype %s', type(data))


def _flat_and_send(nested_dict, expanded_key, sep='.'):
    """
    _flat_and_send

    :param nested_dict:
    :param expanded_key:
    :param sep:
    """
    if not isinstance(nested_dict, dict):
        value = {expanded_key: nested_dict}
        yield value
    else:
        for key, value in nested_dict.iteritems():
            new_key = expanded_key + sep + key if expanded_key else key
            for value in _flat_and_send(value, new_key, sep):
                yield value


def get_slaves(url, user, password, search='', sort_by='status'):
    """
    get_slaves

    :param url:
    :param user:
    :param password:
    :param search:
    :param sort_by:
    """

    slaves = _collect_slaves(url, user, password)
    sorted_slaves = sorted(slaves, key=operator.itemgetter(sort_by.lower(),
                                                           'hostname',
                                                           'idle',
                                                           'label'),
                           reverse=True)
    if search:
        matcher = re.match(r'^(?P<key>.*)~(?P<value>.*)$', search)
        if matcher is None or len(matcher.groups()) != 2:
            key = 'hostname'
            value = search.strip()
        else:
            key = matcher.group('key').strip()
            value = matcher.group('value').strip()
        slaves = (x for x in sorted_slaves if x.get(key, '') and
                  x[key].find(value) != -1)
    else:
        slaves = sorted_slaves

    return slaves


def slaves_histogram(slaves):
    """
    slaves_histogram

    :param slaves:
    """
    histo = {}
    total_online = 0
    total_idle = 0
    for slave in slaves:
        if slave['status'] == 'online':
            total_online += 1
            if slave['idle'] == 'True':
                total_idle += 1
        labels = slave['label'].split(' ')
        for label in labels:
            if histo.get(label):
                histo[label]['total'] = str(int(histo[label]['total']) + 1)
                if slave['idle'] == 'True' and slave['status'] == 'online':
                    histo[label]['idle'] = str(int(histo[label]['idle']) + 1)
            else:
                if slave['idle'] == 'True' and slave['status'] == 'online':
                    histo[label] = {'total': '1', 'idle': '1'}
                else:
                    histo[label] = {'total': '1', 'idle': '0'}
    total = len(slaves)
    totals = {'total': total, 'online': total_online, 'idle': total_idle}
    data = {'totals': totals, 'labels': histo}
    return data


def _collect_slaves(url, user, password):
    """
    _collect_slaves

        :param url:
        :param user:
    :param password:
    """
    j = jenkins.Jenkins(url, user, password)
    nodes_name = j.get_nodes()
    nodes_metadata = []
    for node in nodes_name:
        if node['name'] != 'master':
            node_md = {}
            node_md['status'] = ('online' if node['offline'] is False
                                 else 'offline')
            node_config = xmltodict.parse(j.get_node_config(name=node['name']))
            node_info = j.get_node_info(name=node['name'])
            node_md['name'] = node['name']
            node_md['remoteFS'] = node_config['slave']['remoteFS']
            node_md['executors'] = node_config['slave']['numExecutors']
            node_md['label'] = node_config['slave']['label']
            node_md['idle'] = str(node_info['idle'])
            launcher = node_config['slave']['launcher']['@class']
            if launcher == 'hudson.plugins.sshslaves.SSHLauncher':
                hostname = node_config['slave']['launcher']['host']
                node_md['hostname'] = hostname
            nodes_metadata.append(node_md)
    return nodes_metadata


def get_queue(url, user, password):
    """
    get_queue

    :param url:
    :param user:
    :param password:
    """
    j = jenkins.Jenkins(url, user, password)
    raw_queue = j.get_queue_info()
    queue = {}
    queue['jobs'] = []
    queue['labels'] = {}
    qinfo = QueueInfo()
    for job in raw_queue:
        job_info = {}
        job_info['job_name'] = job['task']['name']
        job_info['reason'] = job['why']
        waiting_time = abs(int(time.time()) -
                           job['inQueueSince'] // 1000) // 60
        job_info['waiting_time'] = waiting_time
        labels = _get_job_label(job['task']['name'],
                                url, user, password)
        job_info['waiting_for'] = labels
        qinfo.labels[labels] = qinfo.labels.setdefault(labels, 0) + 1
        queue['jobs'].append(job_info)

    sorted_queue = sorted(queue['jobs'],
                          key=operator.itemgetter('waiting_time'),
                          reverse=True)
    qinfo.jobs = sorted_queue
    qinfo.total = len(sorted_queue)

    return qinfo


@lru_cache(maxsize=128)
def _get_job_label(job_name, url, user, password):
    """
    _get_job_label

    :param job_name:
    :param url:
    :param user:
    :param password:
    """
    job_config = _get_job_config(job_name, url, user, password)
    matcher = re.search(r'<assignedNode>(?P<labels>.*)</assignedNode>',
                        job_config)

    if matcher:
        res = matcher.group('labels')
        res = res.replace('||', 'or')
        res = res.replace('&amp;&amp;', 'and').strip().replace(' ', '_')
        return res
    else:
        return 'no_label'


@lru_cache(maxsize=128)
def _get_job_config(job_name, url, user, password):
    """
    _get_job_config

    :param job_name:
    :param url:
    :param user:
    :param password:
    """
    j = jenkins.Jenkins(url, user, password)
    job_config = j.get_job_config(job_name)
    return job_config


def get_running_builds(url, user, password):
    """
    get_running_builds

    :param url:
    :param user:
    :param password:
    """
    j = jenkins.Jenkins(url, user, password)
    running_builds = j.get_running_builds()
    jobs_count = Counter(rb['name'] for rb in running_builds)
    data = [('jobs.%s.running' % v[0].replace('.', '_'), v[1])
            for v in jobs_count.viewitems()]
    labels = Counter([_get_job_label(rb['name'], url, user, password)
                      for rb in running_builds])
    builds_labels = [('builds.label.%s.running' % v[0], v[1])
                     for v in labels.viewitems()]
    data.append(('builds.total.running', len(running_builds)))
    data.extend(builds_labels)
    return data


def get_internal_stats(cache_renew, sample_rate, time_to_send):
    """
    get_internal_stats

    :param cache_renew:
    :param sample_rate:
    :param time:
    """
    data = []
    data.append(('internal.cache_renew', cache_renew))
    data.append(('internal.sample_rate', sample_rate))
    data.append(('internal.sending_time', time_to_send))
    return data


def main():
    """main"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--jenkins_url', required=True, help='Jenkins url')
    parser.add_argument('--graphite_host', default='localhost',
                        help='Graphite hostname')
    parser.add_argument('--jenkins_user', default='graphite',
                        help='Jenkins user')
    parser.add_argument('--jenkins_pass', default='', help='Jenkins pass')
    parser.add_argument('--interval', default='30',
                        help='In what interval to collect stats', type=float)
    parser.add_argument('--prefix', default='jenkins',
                        help='Group name prefix for metrics sent to graphite')
    parser.add_argument('--cache_renew',
                        help=('after how many iterations to renew the cache,'
                              'defaults to estimated 24 hours'),
                        type=int)
    parser.add_argument('--log_file', help='where to write the logfile',
                        default='/var/log/jenkins_graphite.log')
    args = parser.parse_args()

    log_formatter = '%(asctime)s - %(levelname)s - %(message)s'
    logging.basicConfig(filename=args.log_file, level=(logging.DEBUG
                                                       if sys.flags.debug
                                                       else logging.INFO),
                        format=log_formatter)
    logging.info('PID: %s, started session', os.getpid())
    logging.info(', '.join(['%s: %s' % (arg, getattr(args, arg))
                            for arg in vars(args)
                            if arg.find('pass') == -1]))
    cache_renew = (round((24 * 60 * 60) / args.interval)
                   if args.cache_renew is None
                   else args.cache_renew)
    cache_counter = 0
    while True:
        try:
            begin = time.clock()
            qinfo = get_queue(args.jenkins_url, args.jenkins_user,
                              args.jenkins_pass)
            send_graphite(qinfo.labels, args.graphite_host,
                          args.prefix + '.inqueue')
            send_graphite({'total': qinfo.total}, args.graphite_host,
                          args.prefix + '.inqueue')
            slaves = get_slaves(args.jenkins_url, args.jenkins_user,
                                args.jenkins_pass)
            send_graphite(slaves_histogram(slaves), args.graphite_host,
                          args.prefix + '.slaves')
            send_graphite(get_running_builds(args.jenkins_url,
                                             args.jenkins_user,
                                             args.jenkins_pass),
                          args.graphite_host,
                          args.prefix)
            cache_counter += 1
            send_graphite(get_internal_stats(cache_renew - cache_counter,
                                             args.interval,
                                             time.clock() - begin),
                          args.graphite_host,
                          args.prefix)
            if cache_counter == cache_renew:
                _get_job_label.cache_clear()
                _get_job_config.cache_clear()
                logging.info('cache flushed, %s iterations' % cache_counter)
                cache_counter = 0
        except Exception:
            logging.exception('entering loop again after exception')
            pass
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
