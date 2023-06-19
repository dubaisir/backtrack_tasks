import datetime
import importlib
import json
import os
import random
import re
import time
from conf import work_dir, log_max_line
import pandas as pd
import threading
import requests
# pd.set_option('display.max_columns', None)  # 显示所有列
# pd.set_option('display.expand_frame_repr', False)  # 禁止自动换行
from flask import Flask, request, render_template
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

task_log_df = pd.DataFrame(columns=['id', 'task_type', 'task_name', 'event_day', 'start_time', 'end_time', 'runtime', 'status', 'submitter'])
futures = {}
task_log_name = pd.DataFrame(columns=['task_name'])
# 在全局范围定义一个锁对象
task_log_lock = threading.Lock()
id_lock = threading.Lock()
id = 0


def get_queue_status():
    url = 'http:'
    req = requests.get(url)
    req_json = req.json()
    queue_info = req_json['queues'][0]

    usedCpuPercent = int(float(re.findall(r'\d+\.\d+|\d+', queue_info['usedCpuPercent'])[0]))
    usedMemPercent = int(float(re.findall(r'\d+\.\d+|\d+', queue_info['usedMemPercent'])[0]))
    if usedCpuPercent <= 75 and usedMemPercent <= 75:
        return 1
    return 0


def alarm(group_id, content, user):
    headers = {'Content-Type': 'application/json'}
    json_data = {
        'message':
            {'header': {'toid': [group_id]},
             'body': [
                 {'content': content,
                  'type': 'TEXT',
                  },
                 {'atuserids': [user],
                  'atall': False,
                  'type': 'AT',
                  }],
             }
    }
    response = requests.post('http:', headers=headers, json=json_data)
    return response.json()


@app.route('/')
def index():
    # 动态加载 config.py 文件
    config = importlib.import_module('conf')
    config = importlib.reload(config)
    work_dir = config.work_dir
    return render_template('index.html', work_dir=work_dir)


# 执行任务的函数
def execute_task(id, task_type, task_name, event_day, submitter, is_retry, concurrency):
    global task_log_df  # 使用global关键字访问全局的task_log_df DataFrame
    global task_log_name
    # 记录任务开始时间
    start_time = datetime.datetime.now()
    # 将任务状态设置为"Running"
    status = 'Running'

    task_type_pattern = r'^[a-zA-Z0-9_]+$'  # 任务类型的正则表达式模式，只允许字母、数字和下划线
    task_name_pattern = r'^[a-zA-Z0-9_./]+$'  # 任务名称的正则表达式模式，只允许字母、数字和下划线
    event_day_pattern = r'^\d{4}\d{2}\d{2}$'  # 事件日期的正则表达式模式，格式为YYYYMMDD
    if re.match(task_type_pattern, task_type) and re.match(task_name_pattern, task_name) and re.match(event_day_pattern, event_day) and (task_log_df.loc[(task_log_df['id'] == id), 'status'] != 'Running').all():
        if not is_retry:
            with task_log_lock:
                task_log_df = pd.concat(
                    [pd.read_json(
                        json.dumps([{'id': id, 'task_type': task_type, 'task_name': task_name, 'event_day': event_day, 'start_time': start_time.strftime('%Y-%m-%d %H:%M:%S'), 'end_time': None, 'runtime': None, 'status': status,
                                     'submitter': submitter}])),
                        task_log_df])
                task_log_df['start_time'] = pd.to_datetime(task_log_df['start_time'])
                task_log_df = task_log_df.sort_values(by=['id'], ascending=[False])
                task_log_df = task_log_df.head(log_max_line)
                task_log_name = task_log_df[["task_name"]].drop_duplicates()
        else:
            with task_log_lock:
                task_log_df.loc[task_log_df['id'] == id, 'start_time'] = start_time.strftime('%Y-%m-%d %H:%M:%S')
                task_log_df.loc[task_log_df['id'] == id, 'end_time'] = None
        while True:
            if get_queue_status() == 1:
                with task_log_lock:
                    task_log_df.loc[task_log_df['id'] == id, 'status'] = status
                break
            else:
                with task_log_lock:
                    task_log_df.loc[task_log_df['id'] == id, 'status'] = 'Queue'
                time.sleep(random.randint(10, 300))
        command = "cd %s && %s %s %s" % (work_dir[submitter], task_type, task_name, event_day)
        print(datetime.datetime.now(), command)
        return_code = os.system(command)
        # 记录任务结束时间
        end_time = datetime.datetime.now()
        runtime = end_time - start_time

        # 根据返回值判断任务状态
        if return_code == 0:
            status = 'Success'
        else:
            status = 'Failed'
            try:
                content = '回溯任务：%s %s 日 失败\n' % (task_name, event_day)
                # alarm(8096298, content, submitter)
            except Exception as e:
                pass

        with task_log_lock:
            # 更新task_log_df DataFrame中的任务信息
            task_log_df.loc[task_log_df['id'] == id, 'end_time'] = end_time.strftime('%Y-%m-%d %H:%M:%S')
            task_log_df.loc[task_log_df['id'] == id, 'runtime'] = runtime
            task_log_df.loc[task_log_df['id'] == id, 'status'] = status

            # 并发为1 任务失败取消后续任务
            if status == 'Failed' and concurrency == 1 and task_name in futures:
                for future in futures[task_name]:
                    future.cancel()

            # 返回任务执行结果

        return return_code
    else:
        print("Invalid task_type, task_name, or event_day.")


@app.route('/submit_task', methods=['POST'])
def submit_task():
    global id
    data = request.get_json()
    task_type = data['task_type']
    task_name = data['task_name']
    task_cycle = data['task_cycle']
    submitter = data['submitter']
    start_date = datetime.datetime.strptime(data['start_date'], '%Y-%m-%d')
    end_date = datetime.datetime.strptime(data['end_date'], '%Y-%m-%d')
    concurrency = int(data['concurrency'])
    date_range = end_date - start_date
    num_days = date_range.days + 1
    # 提交任务到线程池
    concurrency = 15 if concurrency >= 15 else concurrency
    futures[task_name] = []
    print(f" Task {task_type} {task_name} {start_date} {end_date} is executed with concurrency {concurrency}")
    with ThreadPoolExecutor(max_workers=concurrency) as t:
        current_date = start_date
        while current_date <= end_date:
            with id_lock:
                id += 1
            if task_cycle == '日':
                process = t.submit(execute_task, id, task_type, task_name, current_date.strftime('%Y%m%d'), submitter, False, concurrency)
                futures[task_name].append(process)
                current_date += datetime.timedelta(days=1)
            elif task_cycle == '月':
                current_date = current_date.replace(day=1)
                end_date = end_date.replace(day=1)
                process = t.submit(execute_task, id, task_type, task_name, current_date.strftime('%Y%m%d'), submitter, False, concurrency)
                futures[task_name].append(process)
                from dateutil.relativedelta import relativedelta
                current_date += relativedelta(months=1)
    return 'Task submitted.'


@app.route('/get_task_result', methods=['GET'])
def get_task_result():
    return render_template('task_result.html', task_results=task_log_df, task_names=task_log_name)


@app.route('/kill', methods=['POST'])
def kill():
    task_name = request.json['task_name']
    event_day = request.json['event_day']

    cmd = '''ps ux | grep -v grep | grep "%s %s" | awk '{print $2}' | xargs kill -9''' % (task_name, event_day)
    task_name_pattern = r'^[a-zA-Z0-9_.]+$'  # 任务名称的正则表达式模式，只允许字母、数字和下划线
    event_day_pattern = r'^\d{4}\d{2}\d{2}$'  # 事件日期的正则表达式模式，格式为YYYYMMDD

    if re.match(task_name_pattern, task_name) and re.match(event_day_pattern, event_day):
        os.system(cmd)

    if task_name in futures:
        for future in futures[task_name]:
            future.cancel()
        return 'Task {} has been killed.'.format(task_name)
    else:
        return 'Task {} not found.'.format(task_name)


@app.route('/retry', methods=['POST'])
def retry():
    task_name = request.json['task_name']
    event_day = request.json['event_day']
    submitter = request.json['submitter']
    id = int(request.json['id'])
    task_type = request.json['task_type']
    with ThreadPoolExecutor(max_workers=1) as t:
        t.submit(execute_task, id, task_type, task_name, event_day, submitter, True, 1)
    return "retried"


@app.route('/clear', methods=['POST'])
def clear():
    global task_log_df
    if any(status in ['Running', 'Queue'] for status in task_log_df['status']):
        return "Cannot clear logs while there are tasks in 'running' or 'queue' status."
    else:
        task_log_df = pd.DataFrame(columns=['id', 'task_type', 'task_name', 'event_day', 'start_time', 'end_time', 'runtime', 'status', 'submitter'])
    return "clear ok"


if __name__ == '__main__':
    app.run(debug=True, port=17216)
    # app.run(host='0.0.0.0', port=17216)
