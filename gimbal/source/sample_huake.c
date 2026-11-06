// /*
//  * Copyright 2021 Rockchip Electronics Co. LTD
//  *
//  * Licensed under the Apache License, Version 2.0 (the "License");
//  * you may not use this file except in compliance with the License.
//  * You may obtain a copy of the License at
//  *
//  *      http://www.apache.org/licenses/LICENSE-2.0
//  *
//  * Unless required by applicable law or agreed to in writing, software
//  * distributed under the License is distributed on an "AS IS" BASIS,
//  * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  * See the License for the specific language governing permissions and
//  * limitations under the License.
//  *
//  */
// #ifdef __cplusplus
// #if __cplusplus
// #include "runtracker.hpp"
// extern "C" {
// #endif
// #endif /* End of #ifdef __cplusplus */

// #include <assert.h>
// #include <errno.h>
// #include <fcntl.h>
// #include <getopt.h>
// #include <pthread.h>
// #include <signal.h>
// #include <stdbool.h>
// #include <stdio.h>
// #include <stdlib.h>
// #include <string.h>
// #include <sys/prctl.h>
// #include <time.h>
// #include <unistd.h>

// #include "rtsp_demo.h"
// #include "sample_comm.h"

// #include "opencv_wrapper.h"

// rtsp_demo_handle g_rtsplive = NULL;
// static rtsp_session_handle g_rtsp_session;

// typedef struct _rkMpiCtx {
// 	SAMPLE_VI_CTX_S vi;
// 	SAMPLE_VO_CTX_S vo;
// 	SAMPLE_VPSS_CTX_S vpss;
// 	SAMPLE_VENC_CTX_S venc;
// 	SAMPLE_RGN_CTX_S rgn[2];
// } SAMPLE_MPI_CTX_S;

// static bool quit = false;
// static void sigterm_handler(int sig) {
// 	fprintf(stderr, "signal %d\n", sig);
// 	quit = true;
// }

// static RK_CHAR optstr[] = "?::a::b:w:h:l:o:e:d:D:I:i:L:M:F:";
// static const struct option long_options[] = {
//     {"aiq", optional_argument, NULL, 'a'},
//     {"bitrate", required_argument, NULL, 'b'},
//     {"device_name", required_argument, NULL, 'd'},
//     {"width", required_argument, NULL, 'w'},
//     {"height", required_argument, NULL, 'h'},
//     {"input_bmp_name", required_argument, NULL, 'i'},
//     {"loop_count", required_argument, NULL, 'l'},
//     {"output_path", required_argument, NULL, 'o'},
//     {"encode", required_argument, NULL, 'e'},
//     {"disp_devid", required_argument, NULL, 'D'},
//     {"camid", required_argument, NULL, 'I'},
//     {"multictx", required_argument, NULL, 'M'},
//     {"fps", required_argument, NULL, 'F'},
//     {"hdr_mode", required_argument, NULL, 'h' + 'm'},
//     {"help", optional_argument, NULL, '?'},
//     {NULL, 0, NULL, 0},
// };

// /******************************************************************************
//  * function : show usage
//  ******************************************************************************/
// static void print_usage(const RK_CHAR *name) {
// 	printf("usage example:\n");
// 	printf("\t%s -w 1920 -h 1080 -F 30 -a /home/iqfiles/ -I 0 -e h264cbr -b 4096 "
// 	       "-D 0 -i /usr/share/image.bmp -o /data/\n",
// 	       name);
// 	printf("\trtsp://xx.xx.xx.xx/live/0, Default OPEN\n");
// #ifdef RKAIQ
// 	printf("\t-a | --aiq: enable aiq with dirpath provided, eg:-a "
// 	       "/home/iqfiles/, "
// 	       "set dirpath empty to using path by default, without this option aiq "
// 	       "should run in other application\n");
// 	printf("\t-M | --multictx: switch of multictx in isp, set 0 to disable, set "
// 	       "1 to enable. Default: 0\n");
// #endif
// 	printf("\t-d | --device_name: set pcDeviceName, eg: /dev/video0 Default "
// 	       "NULL\n");
// 	printf("\t-I | --camid: camera ctx id, Default 0\n");
// 	printf("\t-w | --width: camera with, Default 1920\n");
// 	printf("\t-h | --height: camera height, Default 1080\n");
// 	printf("\t-e | --encode: encode type, Default:h264cbr, Value:h264cbr, "
// 	       "h264vbr, h264avbr "
// 	       "h265cbr, h265vbr, h265avbr, mjpegcbr, mjpegvbr\n");
// 	printf("\t-b | --bitrate: encode bitrate, Default 4096\n");
// 	printf("\t-i | --input_bmp_name: input file path of logo.bmp, Default NULL\n");
// 	printf("\t-l | --loop_count: loop count, Default -1\n");
// 	printf("\t-o | --output_path: encode save file path, Default /data/\n");
// 	printf("\t-D | --disp_devid: display DevId, Default -1\n");
// 	printf("\t-F | --fps: disp_layer fps, Default 30\n");
// }

// RK_U64 SAMPLE_COMM_VI_Frame(SAMPLE_VI_CTX_S *ctx, void **pdata) {
// 	// RK_S32 s32Ret = RK_FAILURE;

// 	*pdata = RK_MPI_MB_Handle2VirAddr(ctx->stViFrame.stVFrame.pMbBlk);
// 	RK_MPI_VI_QueryChnStatus(ctx->u32PipeId, ctx->s32ChnId, &ctx->stChnStatus);
// 	return ctx->stViFrame.stVFrame.u64PrivateData;
// }

// static void *GetMediaBuffer0(void *arg) {
// 	int process_video_width = 1920;
// 	int process_video_height = 1088;
// 	printf("========%s========\n", __func__);
// 	int s32Ret;
// 	SAMPLE_MPI_CTX_S *ctx=(SAMPLE_MPI_CTX_S *)arg;
// 	void *pData = RK_NULL;
// 	RK_U64 len=0;
// 	char name[256] = {0};
// 	FILE *fp = RK_NULL;
// 	// RK_S32 loopCount = 0;

// 	if (ctx->venc.dstFilePath) {
// 		snprintf(name, sizeof(name), "/%s/vi_%d.bin", ctx->venc.dstFilePath, ctx->venc.s32ChnId);
// 		fp = fopen(name, "wb");
// 		if (fp == RK_NULL) {
// 			printf("chn %d can't open %s file !\n", ctx->venc.s32ChnId, ctx->venc.dstFilePath);
// 			quit = true;
// 			return RK_NULL;
// 		}
// 	}

// 	while (!quit) {
// 		s32Ret = RK_MPI_VI_GetChnFrame(ctx->vi.s32DevId, ctx->vi.s32ChnId, &ctx->vi.stViFrame, -1);
// 		if (s32Ret == RK_SUCCESS) {

// 			//手动的进行地址转换
// 			len = SAMPLE_COMM_VI_Frame(&ctx->vi,&pData);
// 			printf("len:%d\n", len);
// 			// processRGBData((unsigned char*)pData, process_video_width, process_video_height);

// 			if (fp) {
// 				fwrite( &ctx->vi.stViFrame, 1, len, fp);
// 				fflush(fp);
// 			}

// 			draw_text_on_image((unsigned char*)pData,
//                                ctx->vi.stViFrame.stVFrame.u32Width,
//                                ctx->vi.stViFrame.stVFrame.u32Height,
//                                "Hello RK");

// 			RK_MPI_VPSS_SendFrame(ctx->vpss.s32GrpId,ctx->vpss.s32ChnId,&ctx->vi.stViFrame,-1);

// 			s32Ret = RK_MPI_VI_ReleaseChnFrame(ctx->vi.s32DevId, ctx->vi.s32ChnId,&ctx->vi.stViFrame);
// 			if (s32Ret != RK_SUCCESS) {
// 				RK_LOGE("RK_MPI_VI_ReleaseChnFrame fail %x", s32Ret);
// 			}
// 		} else {
// 			RK_LOGE("RK_MPI_VI_Release ChnFrame timeout %x", s32Ret);
// 		}
// 		usleep(1000);
// 	}

// 	if (fp)
// 	fclose(fp);

// 	return NULL;
// }

// /******************************************************************************
//  * function : venc thread
//  ******************************************************************************/
// static void *venc_get_stream(void *pArgs) {
// 	SAMPLE_VENC_CTX_S *ctx = (SAMPLE_VENC_CTX_S *)(pArgs);
// 	RK_S32 s32Ret = RK_FAILURE;
// 	char name[256] = {0};
// 	FILE *fp = RK_NULL;
// 	void *pData = RK_NULL;
// 	RK_S32 loopCount = 0;

// 	if (ctx->dstFilePath) {
// 		snprintf(name, sizeof(name), "/%s/venc_%d.bin", ctx->dstFilePath, ctx->s32ChnId);
// 		fp = fopen(name, "wb");
// 		if (fp == RK_NULL) {
// 			printf("chn %d can't open %s file !\n", ctx->s32ChnId, ctx->dstFilePath);
// 			quit = true;
// 			return RK_NULL;
// 		}
// 	}

// 	while (!quit) {
// 		s32Ret = SAMPLE_COMM_VENC_GetStream(ctx, &pData);
// 		if (s32Ret == RK_SUCCESS) {
// 			// exit when complete
// 			if (ctx->s32loopCount > 0) {
// 				if (loopCount >= ctx->s32loopCount) {
// 					SAMPLE_COMM_VENC_ReleaseStream(ctx);
// 					quit = true;
// 					break;
// 				}
// 			}

// 			if (fp) {
// 				fwrite(pData, 1, ctx->stFrame.pstPack->u32Len, fp);
// 				fflush(fp);
// 			}

// 			PrintStreamDetails(ctx->s32ChnId, ctx->stFrame.pstPack->u32Len);
// 			rtsp_tx_video(g_rtsp_session, pData, ctx->stFrame.pstPack->u32Len,
// 			              ctx->stFrame.pstPack->u64PTS);
// 			rtsp_do_event(g_rtsplive);

// 			RK_LOGD("chn:%d, loopCount:%d wd:%d\n", ctx->s32ChnId, loopCount,
// 			        ctx->stFrame.pstPack->u32Len);

// 			SAMPLE_COMM_VENC_ReleaseStream(ctx);
// 			loopCount++;
// 		}
// 		usleep(1000);
// 	}

// 	if (fp)
// 		fclose(fp);

// 	return RK_NULL;
// }

// /******************************************************************************
//  * function    : main()
//  * Description : main
//  ******************************************************************************/
// int main(int argc, char *argv[]) {
// 	SAMPLE_MPI_CTX_S *ctx;
// 	int video_width = 1920;
// 	int video_height = 1080;
// 	int venc_width = video_width;
// 	int venc_height = video_height;
// 	int disp_width = video_width;
// 	int disp_height = video_height;
// 	int disp_fps = 30;
// 	RK_CHAR *pDeviceName = NULL;
// 	RK_CHAR *pInPathBmp = NULL;
// 	RK_CHAR *pOutPathVenc = NULL;
// 	CODEC_TYPE_E enCodecType = RK_CODEC_TYPE_H264;
// 	VENC_RC_MODE_E enRcMode = VENC_RC_MODE_H264CBR;
// 	RK_CHAR *pCodecName = "H264";
// 	RK_S32 s32CamId = 0;
// 	RK_S32 s32DisId = -1;
// 	RK_S32 s32DisLayerId = 0;
// 	RK_S32 s32loopCnt = -1;
// 	RK_S32 s32BitRate = 4 * 1024;
// 	MPP_CHN_S stSrcChn, stDestChn;

// 	if (argc < 2) {
// 		print_usage(argv[0]);
// 		return 0;
// 	}

// 	ctx = (SAMPLE_MPI_CTX_S *)(malloc(sizeof(SAMPLE_MPI_CTX_S)));
// 	memset(ctx, 0, sizeof(SAMPLE_MPI_CTX_S));

// 	signal(SIGINT, sigterm_handler);

// #ifdef RKAIQ
// 	RK_BOOL bMultictx = RK_FALSE;
// #endif
// 	int c;
// 	char *iq_file_dir = NULL;
// 	while ((c = getopt_long(argc, argv, optstr, long_options, NULL)) != -1) {
// 		const char *tmp_optarg = optarg;
// 		switch (c) {
// 		case 'a':
// 			if (!optarg && NULL != argv[optind] && '-' != argv[optind][0]) {
// 				tmp_optarg = argv[optind++];
// 			}
// 			if (tmp_optarg) {
// 				iq_file_dir = (char *)tmp_optarg;
// 			} else {
// 				iq_file_dir = NULL;
// 			}
// 			break;
// 		case 'b':
// 			s32BitRate = atoi(optarg);
// 			break;
// 		case 'd':
// 			pDeviceName = optarg;
// 			break;
// 		case 'D':
// 			s32DisId = atoi(optarg);
// 			break;
// 		case 'e':
// 			if (!strcmp(optarg, "h264cbr")) {
// 				enCodecType = RK_CODEC_TYPE_H264;
// 				enRcMode = VENC_RC_MODE_H264CBR;
// 				pCodecName = "H264";
// 			} else if (!strcmp(optarg, "h264vbr")) {
// 				enCodecType = RK_CODEC_TYPE_H264;
// 				enRcMode = VENC_RC_MODE_H264VBR;
// 				pCodecName = "H264";
// 			} else if (!strcmp(optarg, "h264avbr")) {
// 				enCodecType = RK_CODEC_TYPE_H264;
// 				enRcMode = VENC_RC_MODE_H264AVBR;
// 				pCodecName = "H264";
// 			} else if (!strcmp(optarg, "h265cbr")) {
// 				enCodecType = RK_CODEC_TYPE_H265;
// 				enRcMode = VENC_RC_MODE_H265CBR;
// 				pCodecName = "H265";
// 			} else if (!strcmp(optarg, "h265vbr")) {
// 				enCodecType = RK_CODEC_TYPE_H265;
// 				enRcMode = VENC_RC_MODE_H265VBR;
// 				pCodecName = "H265";
// 			} else if (!strcmp(optarg, "h265avbr")) {
// 				enCodecType = RK_CODEC_TYPE_H265;
// 				enRcMode = VENC_RC_MODE_H265AVBR;
// 				pCodecName = "H265";
// 			} else if (!strcmp(optarg, "mjpegcbr")) {
// 				enCodecType = RK_CODEC_TYPE_MJPEG;
// 				enRcMode = VENC_RC_MODE_MJPEGCBR;
// 				pCodecName = "MJPEG";
// 			} else if (!strcmp(optarg, "mjpegvbr")) {
// 				enCodecType = RK_CODEC_TYPE_MJPEG;
// 				enRcMode = VENC_RC_MODE_MJPEGVBR;
// 				pCodecName = "MJPEG";
// 			} else {
// 				printf("ERROR: Invalid encoder type.\n");
// 				return 0;
// 			}
// 			break;
// 		case 'w':
// 			video_width = atoi(optarg);
// 			venc_width = video_width;
// 			disp_width = video_width;
// 			break;
// 		case 'h':
// 			video_height = atoi(optarg);
// 			venc_height = video_height;
// 			disp_height = video_height;
// 			break;
// 		case 'F':
// 			disp_fps = atoi(optarg);
// 			break;
// 		case 'I':
// 			s32CamId = atoi(optarg);
// 			break;
// 		case 'i':
// 			pInPathBmp = optarg;
// 			break;
// 		case 'l':
// 			s32loopCnt = atoi(optarg);
// 			break;
// 		case 'L':
// 			s32DisLayerId = atoi(optarg);
// 			break;
// 		case 'o':
// 			pOutPathVenc = optarg;
// 			break;
// #ifdef RKAIQ
// 		case 'M':
// 			if (atoi(optarg)) {
// 				bMultictx = RK_TRUE;
// 			}
// 			break;
// #endif
// 		case '?':
// 		default:
// 			print_usage(argv[0]);
// 			return 0;
// 		}
// 	}

// 	printf("#CameraIdx: %d\n", s32CamId);
// 	printf("#pDeviceName: %s\n", pDeviceName);
// 	printf("#CodecName:%s\n", pCodecName);
// 	printf("#Output Path: %s\n", pOutPathVenc);
// 	printf("#IQ Path: %s\n", iq_file_dir);
// 	if (iq_file_dir) {
// #ifdef RKAIQ
// 		printf("#Rkaiq XML DirPath: %s\n", iq_file_dir);
// 		printf("#bMultictx: %d\n\n", bMultictx);
// 		rk_aiq_working_mode_t hdr_mode = RK_AIQ_WORKING_MODE_NORMAL;

// 		SAMPLE_COMM_ISP_Init(s32CamId, hdr_mode, bMultictx, iq_file_dir);
// 		SAMPLE_COMM_ISP_Run(s32CamId);
// #endif
// 	}

// 	// init rtsp
// 	g_rtsplive = create_rtsp_demo(554);
// 	g_rtsp_session = rtsp_new_session(g_rtsplive, "/live/0");
// 	if (enCodecType == RK_CODEC_TYPE_H264) {
// 		rtsp_set_video(g_rtsp_session, RTSP_CODEC_ID_VIDEO_H264, NULL, 0);
// 	} else if (enCodecType == RK_CODEC_TYPE_H265) {
// 		rtsp_set_video(g_rtsp_session, RTSP_CODEC_ID_VIDEO_H265, NULL, 0);
// 	} else {
// 		printf("not support other type\n");
// 		return -1;
// 	}
// 	rtsp_sync_video_ts(g_rtsp_session, rtsp_get_reltime(), rtsp_get_ntptime());

// 	if (RK_MPI_SYS_Init() != RK_SUCCESS) {
// 		goto __FAILED;
// 	}

// 	// Init VI[0]
// 	ctx->vi.u32Width = video_width;
// 	ctx->vi.u32Height = video_height;
// 	ctx->vi.s32DevId = s32CamId;
// 	ctx->vi.u32PipeId = ctx->vi.s32DevId;
// 	ctx->vi.s32ChnId = 1;
// 	ctx->vi.stChnAttr.stIspOpt.u32BufCount = 3;
// 	ctx->vi.stChnAttr.stIspOpt.enMemoryType = VI_V4L2_MEMORY_TYPE_DMABUF;
// 	ctx->vi.stChnAttr.u32Depth = 1;
// 	ctx->vi.stChnAttr.enPixelFormat = RK_FMT_YUV420SP;
// 	ctx->vi.stChnAttr.stFrameRate.s32SrcFrameRate = -1;
// 	ctx->vi.stChnAttr.stFrameRate.s32DstFrameRate = -1;
// 	if (pDeviceName) {
// 		strcpy(ctx->vi.stChnAttr.stIspOpt.aEntityName, pDeviceName);
// 	}
// 	SAMPLE_COMM_VI_CreateChn(&ctx->vi);

// 	// Init VPSS[0]
// 	ctx->vpss.s32GrpId = 0;
// 	ctx->vpss.s32ChnId = 0;
// 	// RGA_device: VIDEO_PROC_DEV_RGA GPU_device: VIDEO_PROC_DEV_GPU
// 	ctx->vpss.enVProcDevType = VIDEO_PROC_DEV_RGA;
// 	ctx->vpss.stGrpVpssAttr.enPixelFormat = RK_FMT_YUV420SP;
// 	ctx->vpss.stGrpVpssAttr.enCompressMode = COMPRESS_MODE_NONE; // no compress

// 	ctx->vpss.stCropInfo.bEnable = RK_FALSE;
// 	ctx->vpss.stCropInfo.enCropCoordinate = VPSS_CROP_RATIO_COOR;
// 	ctx->vpss.stCropInfo.stCropRect.s32X = 0;
// 	ctx->vpss.stCropInfo.stCropRect.s32Y = 0;
// 	ctx->vpss.stCropInfo.stCropRect.u32Width = video_width;
// 	ctx->vpss.stCropInfo.stCropRect.u32Height = video_height;

// 	ctx->vpss.stChnCropInfo[0].bEnable = RK_TRUE;
// 	ctx->vpss.stChnCropInfo[0].enCropCoordinate = VPSS_CROP_RATIO_COOR;
// 	ctx->vpss.stChnCropInfo[0].stCropRect.s32X = 0;
// 	ctx->vpss.stChnCropInfo[0].stCropRect.s32Y = 0;
// 	ctx->vpss.stChnCropInfo[0].stCropRect.u32Width = venc_width * 1000 / video_width;
// 	ctx->vpss.stChnCropInfo[0].stCropRect.u32Height = venc_height * 1000 / video_height;
// 	ctx->vpss.s32ChnRotation[0] = ROTATION_180; // ROTATION_90
// 	ctx->vpss.stRotationEx[0].bEnable = RK_FALSE;
// 	ctx->vpss.stRotationEx[0].stRotationEx.u32Angle = 60;
// 	ctx->vpss.stVpssChnAttr[0].enChnMode = VPSS_CHN_MODE_USER;
// 	ctx->vpss.stVpssChnAttr[0].enCompressMode = COMPRESS_MODE_NONE;
// 	ctx->vpss.stVpssChnAttr[0].enDynamicRange = DYNAMIC_RANGE_SDR8;
// 	ctx->vpss.stVpssChnAttr[0].enPixelFormat = RK_FMT_RGB888;
// 	ctx->vpss.stVpssChnAttr[0].stFrameRate.s32SrcFrameRate = -1;
// 	ctx->vpss.stVpssChnAttr[0].stFrameRate.s32DstFrameRate = -1;
// 	ctx->vpss.stVpssChnAttr[0].u32Width = venc_width;
// 	ctx->vpss.stVpssChnAttr[0].u32Height = venc_height;

// 	if (s32DisId >= 0) {
// 		ctx->vpss.s32ChnRotation[1] = ROTATION_0;
// 		ctx->vpss.stVpssChnAttr[1].enChnMode = VPSS_CHN_MODE_USER;
// 		ctx->vpss.stVpssChnAttr[1].enCompressMode = COMPRESS_MODE_NONE;
// 		ctx->vpss.stVpssChnAttr[1].enDynamicRange = DYNAMIC_RANGE_SDR8;
// 		ctx->vpss.stVpssChnAttr[1].enPixelFormat = RK_FMT_YUV420SP;
// 		ctx->vpss.stVpssChnAttr[1].stFrameRate.s32SrcFrameRate = -1;
// 		ctx->vpss.stVpssChnAttr[1].stFrameRate.s32DstFrameRate = -1;
// 		ctx->vpss.stVpssChnAttr[1].u32Width = video_width;
// 		ctx->vpss.stVpssChnAttr[1].u32Height = video_height;
// 	}
// 	SAMPLE_COMM_VPSS_CreateChn(&ctx->vpss);

// 	// Init VENC[0]
// 	ctx->venc.s32ChnId = 0;
// 	ctx->venc.u32Width = venc_width;
// 	ctx->venc.u32Height = venc_height;
// 	ctx->venc.u32Fps = disp_fps;
// 	ctx->venc.u32Gop = disp_fps;
// 	ctx->venc.u32BitRate = s32BitRate;
// 	ctx->venc.enCodecType = enCodecType;
// 	ctx->venc.enRcMode = enRcMode;
// 	ctx->venc.u32StreamBufCnt = 3;
// 	ctx->venc.getStreamCbFunc = venc_get_stream;
// 	ctx->venc.s32loopCount = s32loopCnt;
// 	ctx->venc.dstFilePath = pOutPathVenc;
// 	// H264  66：Baseline  77：Main Profile 100：High Profile
// 	// H265  0：Main Profile  1：Main 10 Profile
// 	// MJPEG 0：Baseline
// 	ctx->venc.stChnAttr.stVencAttr.u32Profile = 100;
// 	ctx->venc.stChnAttr.stGopAttr.enGopMode = VENC_GOPMODE_NORMALP; // VENC_GOPMODE_SMARTP
// 	SAMPLE_COMM_VENC_CreateChn(&ctx->venc);

// 	if (s32DisId >= 0) {
// 		// Init VO[0]
// 		ctx->vo.s32DevId = 0;
// 		ctx->vo.s32ChnId = 0;
// 		ctx->vo.s32LayerId = s32DisLayerId;
// 		ctx->vo.Volayer_mode = VO_LAYER_MODE_GRAPHIC;
// 		ctx->vo.u32DispBufLen = 3;
// 		ctx->vo.stVoPubAttr.enIntfType = VO_INTF_HDMI;
// 		ctx->vo.stVoPubAttr.enIntfSync = VO_OUTPUT_1080P60;
// 		ctx->vo.stLayerAttr.stDispRect.s32X = 0;
// 		ctx->vo.stLayerAttr.stDispRect.s32Y = 0;
// 		ctx->vo.stLayerAttr.stDispRect.u32Width = disp_width;
// 		ctx->vo.stLayerAttr.stDispRect.u32Height = disp_height;
// 		ctx->vo.stLayerAttr.stImageSize.u32Width = disp_width;
// 		ctx->vo.stLayerAttr.stImageSize.u32Height = disp_height;
// 		ctx->vo.stLayerAttr.u32DispFrmRt = disp_fps;
// 		ctx->vo.stLayerAttr.enPixFormat = RK_FMT_RGB888;
// 		// ctx->vo.stLayerAttr.bDoubleFrame = RK_FALSE;
// 		ctx->vo.stChnAttr.stRect.s32X = 0;
// 		ctx->vo.stChnAttr.stRect.s32Y = 0;
// 		ctx->vo.stChnAttr.stRect.u32Width = disp_width;
// 		ctx->vo.stChnAttr.stRect.u32Height = disp_height;
// 		ctx->vo.stChnAttr.u32Priority = 1;
// 		SAMPLE_COMM_VO_CreateChn(&ctx->vo);
// 	}

// 	pthread_t main_thread;
// 	pthread_create(&main_thread, NULL, GetMediaBuffer0,  (void *)ctx);

// 	// // Bind VI[0] and VPSS[0]
// 	// stSrcChn.enModId = RK_ID_VI;
// 	// stSrcChn.s32DevId = ctx->vi.s32DevId;
// 	// stSrcChn.s32ChnId = ctx->vi.s32ChnId;
// 	// stDestChn.enModId = RK_ID_VPSS;
// 	// stDestChn.s32DevId = ctx->vpss.s32GrpId;
// 	// stDestChn.s32ChnId = ctx->vpss.s32ChnId;
// 	// SAMPLE_COMM_Bind(&stSrcChn, &stDestChn);

// 	// Bind VPSS[0] and VENC[0]
// 	stSrcChn.enModId = RK_ID_VPSS;
// 	stSrcChn.s32DevId = ctx->vpss.s32GrpId;
// 	stSrcChn.s32ChnId = ctx->vpss.s32ChnId;
// 	stDestChn.enModId = RK_ID_VENC;
// 	stDestChn.s32DevId = 0;
// 	stDestChn.s32ChnId = ctx->venc.s32ChnId;
// 	SAMPLE_COMM_Bind(&stSrcChn, &stDestChn);

// 	if (s32DisId >= 0) {
// 		// Bind VPSS[1] and VO[0]
// 		stSrcChn.enModId = RK_ID_VPSS;
// 		stSrcChn.s32DevId = ctx->vpss.s32GrpId;
// 		stSrcChn.s32ChnId = 1;
// 		stDestChn.enModId = RK_ID_VO;
// 		stDestChn.s32DevId = ctx->vo.s32LayerId;
// 		stDestChn.s32ChnId = ctx->vo.s32ChnId;
// 		SAMPLE_COMM_Bind(&stSrcChn, &stDestChn);
// 	}

// 	printf("%s initial finish\n", __func__);

// 	while (!quit) {
// 		sleep(1);
// 	}

// 	printf("%s exit!\n", __func__);

// 	if (ctx->venc.getStreamCbFunc) {
// 		pthread_join(ctx->venc.getStreamThread, NULL);
// 	}

// 	if (g_rtsplive)
// 		rtsp_del_demo(g_rtsplive);

// 	if (s32DisId >= 0) {
// 		// UnBind VPSS[0] and VO[0]
// 		stSrcChn.enModId = RK_ID_VPSS;
// 		stSrcChn.s32DevId = ctx->vpss.s32GrpId;
// 		stSrcChn.s32ChnId = 1;
// 		stDestChn.enModId = RK_ID_VO;
// 		stDestChn.s32DevId = ctx->vo.s32LayerId;
// 		stDestChn.s32ChnId = ctx->vo.s32ChnId;
// 		SAMPLE_COMM_UnBind(&stSrcChn, &stDestChn);
// 	}

// 	// UnBind VPSS[0] and VENC[0]
// 	stSrcChn.enModId = RK_ID_VPSS;
// 	stSrcChn.s32DevId = ctx->vpss.s32GrpId;
// 	stSrcChn.s32ChnId = ctx->vpss.s32ChnId;
// 	stDestChn.enModId = RK_ID_VENC;
// 	stDestChn.s32DevId = 0;
// 	stDestChn.s32ChnId = ctx->venc.s32ChnId;
// 	SAMPLE_COMM_UnBind(&stSrcChn, &stDestChn);

// 	// // UnBind VI[0] and VPSS[0]
// 	// stSrcChn.enModId = RK_ID_VI;
// 	// stSrcChn.s32DevId = ctx->vi.s32DevId;
// 	// stSrcChn.s32ChnId = ctx->vi.s32ChnId;
// 	// stDestChn.enModId = RK_ID_VPSS;
// 	// stDestChn.s32DevId = ctx->vpss.s32GrpId;
// 	// stDestChn.s32ChnId = ctx->vpss.s32ChnId;
// 	// SAMPLE_COMM_UnBind(&stSrcChn, &stDestChn);

// 	if (s32DisId >= 0) {
// 		// Destroy VO[0]
// 		SAMPLE_COMM_VO_DestroyChn(&ctx->vo);
// 	}
// 	// Destroy VENC[0]
// 	SAMPLE_COMM_VENC_DestroyChn(&ctx->venc);
// 	// Destroy VPSS[0]
// 	SAMPLE_COMM_VPSS_DestroyChn(&ctx->vpss);
// 	// Destroy VI[0]
// 	SAMPLE_COMM_VI_DestroyChn(&ctx->vi);
// __FAILED:
// 	RK_MPI_SYS_Exit();
// 	if (iq_file_dir) {
// #ifdef RKAIQ
// 		SAMPLE_COMM_ISP_Stop(s32CamId);
// #endif
// 	}
// 	if (ctx) {
// 		free(ctx);
// 		ctx = RK_NULL;
// 	}

// 	return 0;
// }

// #ifdef __cplusplus
// #if __cplusplus
// }
// #endif
// #endif /* End of #ifdef __cplusplus */





/*
 * Copyright 2021 Rockchip Electronics Co. LTD
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */
#ifdef __cplusplus
#if __cplusplus
extern "C" {
#endif
#endif /* End of #ifdef __cplusplus */

#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <time.h>
#include <unistd.h>

#include "rtsp_demo.h"
#include "sample_comm.h"
#include "servo_proto.h"
#include "servo_cmd.h"
#include "net/tcp_cmd_server.h"
#include "tracker/tracker_bridge.h"
#include "tracker/algo_pipeline.h"
#include "video_source_config.h"
#include "mino_ir_stream.h"
#include "low_stream.h"
#include "stream_tuning.h"

rtsp_demo_handle g_rtsplive = NULL;
static rtsp_session_handle g_rtsp_session;
static pthread_mutex_t g_rtsp_mutex = PTHREAD_MUTEX_INITIALIZER;

typedef struct _rkMpiCtx {
	SAMPLE_VI_CTX_S vi;
	SAMPLE_VO_CTX_S vo;
	SAMPLE_VPSS_CTX_S vpss;
	SAMPLE_VENC_CTX_S venc;
	SAMPLE_RGN_CTX_S rgn[2];
} SAMPLE_MPI_CTX_S;

static volatile sig_atomic_t quit = 0;
int g_servo_ok = 0; /* 1 if servo_proto_init succeeded; extern用于 tracker_bridge.c */
static void sigterm_handler(int sig) {
	fprintf(stderr, "signal %d\n", sig);
	quit = true;
}

/* --- Servo receive callback --- */
static void on_servo_rx(uint16_t msgid, uint8_t frame_type,
                        uint8_t *payload, uint8_t len, void *user) {
	(void)user;
	(void)frame_type;

	if (msgid == 0x0002 && len >= sizeof(servo_attitude_report_t)) {
		servo_attitude_report_t *att = (servo_attitude_report_t *)payload;
		tracker_bridge_on_attitude(att->yaw_frame_angle * 0.01,
		                           att->pitch_frame_angle * 0.01,
		                           att->yaw_speed * 0.01,
		                           att->pitch_speed * 0.01, 0);
		/* 每 200 次(约 2 秒@100Hz)打一次诊断 */
		static int att_cnt = 0;
		if ((++att_cnt) % 200 == 0) {
			printf("[GIMBAL] yaw=%.2f pitch=%.2f spd_yaw=%.2f spd_pitch=%.2f trk_off=(%d,%d)\n",
			       att->yaw_angle * 0.01f, att->pitch_angle * 0.01f,
			       att->yaw_speed * 0.01f, att->pitch_speed * 0.01f,
			       att->track_offset_x, att->track_offset_y);
		}
	}
}

/* --- Shared state for remote commands (defined in runtracker.cpp) --- */
extern int select_flag;
extern bool IS_TRACK;
extern int yolo_num_id;
extern int Coordinate_X;
extern int Coordinate_Y;
extern int Selection_X;
extern int Selection_Y;
extern int Selection_W;
extern int Selection_H;
extern pthread_mutex_t g_select_mutex;
extern bool g_unlock_requested;

static void on_tcp_command(uint8_t cmd_id, const void *payload, uint16_t len) {
	switch (cmd_id) {
	case CMD_TRACK_XY: {
		if (len != sizeof(cmd_track_xy_t)) break;
		const cmd_track_xy_t *p = (const cmd_track_xy_t *)payload;
		pthread_mutex_lock(&g_select_mutex);
		select_flag = 1;
		Coordinate_X = p->x;
		Coordinate_Y = p->y;
		IS_TRACK = true;
		tracker_bridge_set_enabled(1);
		pthread_mutex_unlock(&g_select_mutex);
		printf("[TCP] Track XY: (%d, %d)\n", p->x, p->y);
		break;
	}
	case CMD_TRACK_ID: {
		if (len != sizeof(cmd_track_id_t)) break;
		const cmd_track_id_t *p = (const cmd_track_id_t *)payload;
		pthread_mutex_lock(&g_select_mutex);
		select_flag = 2;
		yolo_num_id = p->target_id;
		IS_TRACK = true;
		tracker_bridge_set_enabled(1);
		pthread_mutex_unlock(&g_select_mutex);
		printf("[TCP] Track ID: %d\n", p->target_id);
		break;
	}
	case CMD_TRACK_UNLOCK: {
        if (len != 0) break;
		pthread_mutex_lock(&g_select_mutex);
		IS_TRACK = false;
		tracker_bridge_set_enabled(0);
		g_unlock_requested = true;  // 通知跟踪线程发 CMD_ID_UNLOCK 给 KCF
		pthread_mutex_unlock(&g_select_mutex);
		printf("[TCP] Track UNLOCK\n");
		break;
	}
	case CMD_TRACK_BOX: {
		if (len != sizeof(cmd_track_box_t)) {
			printf("[TCP] Invalid TRACK_BOX length: %u\n", len);
			break;
		}
		cmd_track_box_t box;
		memcpy(&box, payload, sizeof(box));
		if (box.w < 4 || box.h < 4) {
			printf("[TCP] Invalid TRACK_BOX: (%d,%d,%d,%d)\n",
			       box.x, box.y, box.w, box.h);
			break;
		}
		pthread_mutex_lock(&g_select_mutex);
		select_flag = 3;
		Selection_X = box.x;
		Selection_Y = box.y;
		Selection_W = box.w;
		Selection_H = box.h;
		IS_TRACK = true;
		g_unlock_requested = true; /* replace an existing lock on the next frame */
		tracker_bridge_set_enabled(1);
		pthread_mutex_unlock(&g_select_mutex);
		printf("[TCP] Track BOX: (%d, %d, %d, %d)\n",
		       box.x, box.y, box.w, box.h);
		break;
	}
	case CMD_DETECTION_ENABLE: {
		if (len != sizeof(cmd_detection_enable_t)) {
			printf("[TCP] Invalid DETECTION_ENABLE length: %u\n", len);
			break;
		}
		cmd_detection_enable_t control;
		memcpy(&control, payload, sizeof(control));
		if (control.enabled > 1) {
			printf("[TCP] Invalid DETECTION_ENABLE value: %u\n", control.enabled);
			break;
		}
		setDetectionEnabled(control.enabled);
		printf("[TCP] Detection %s\n", control.enabled ? "ON" : "OFF");
		break;
	}
	case CMD_GIMBAL_CENTER: {
        if (len != 0) break;
        pthread_mutex_lock(&g_select_mutex);
        IS_TRACK = false; g_unlock_requested = true;
        pthread_mutex_unlock(&g_select_mutex);
		if (g_servo_ok) {
			tracker_bridge_manual_gimbal(
				SERVO_GIMBAL_MODE_MAKE(SERVO_GIMBAL_WORK_CENTER, SERVO_GIMBAL_FUNC_NONE),
				128, 128);
		}
		printf("[TCP] Gimbal CENTER\n");
		break;
	}
	case CMD_GIMBAL_DOWN90: {
        if (len != 0) break;
        pthread_mutex_lock(&g_select_mutex);
        IS_TRACK = false; g_unlock_requested = true;
        pthread_mutex_unlock(&g_select_mutex);
		if (g_servo_ok)
			tracker_bridge_manual_gimbal(
				SERVO_GIMBAL_MODE_MAKE(SERVO_GIMBAL_WORK_DOWN_90, SERVO_GIMBAL_FUNC_NONE),
				128, 128);
		printf("[TCP] Gimbal DOWN 90\n");
		break;
	}
	case CMD_GIMBAL_SET_ANGLE: {
		if (len != sizeof(cmd_gimbal_angle_t)) break;
        pthread_mutex_lock(&g_select_mutex);
        IS_TRACK = false; g_unlock_requested = true;
        pthread_mutex_unlock(&g_select_mutex);

		const cmd_gimbal_angle_t *p = (const cmd_gimbal_angle_t *)payload;
		if (g_servo_ok)
			tracker_bridge_manual_angle(p->pitch_deg, p->yaw_deg, 0);
		printf("[TCP] Gimbal angle: pitch=%d yaw=%d\n", p->pitch_deg, p->yaw_deg);
		break;
	}
	case CMD_GIMBAL_SPEED: {
		if (len != sizeof(cmd_gimbal_speed_t)) break;
        pthread_mutex_lock(&g_select_mutex);
        IS_TRACK = false; g_unlock_requested = true;
        pthread_mutex_unlock(&g_select_mutex);

		const cmd_gimbal_speed_t *p = (const cmd_gimbal_speed_t *)payload;
		if (g_servo_ok)
			tracker_bridge_manual_gimbal(
				SERVO_GIMBAL_MODE_MAKE(SERVO_GIMBAL_WORK_NONE, SERVO_GIMBAL_FUNC_NONE),
				p->yaw_speed, p->pitch_speed);
		printf("[TCP] Gimbal speed: yaw=%u pitch=%u\n", p->yaw_speed, p->pitch_speed);
		break;
	}
	case CMD_GIMBAL_LOCK: {
		if (len != sizeof(cmd_gimbal_lock_t)) break;
        if (((const cmd_gimbal_lock_t *)payload)->mode > 1) break;
        pthread_mutex_lock(&g_select_mutex);
        IS_TRACK = false; g_unlock_requested = true;
        pthread_mutex_unlock(&g_select_mutex);

		const cmd_gimbal_lock_t *p = (const cmd_gimbal_lock_t *)payload;

		if (g_servo_ok)
			tracker_bridge_manual_lock(p->mode);
		printf("[TCP] Gimbal lock: %u\n", p->mode);
		break;
	}
	default:
		printf("[TCP] Unknown cmd: 0x%02x\n", cmd_id);
		break;
	}
}

static RK_CHAR optstr[] = "?::a::b:w:h:l:o:e:d:D:I:i:L:M:F:S:g:";
static const struct option long_options[] = {
    {"aiq", optional_argument, NULL, 'a'},
    {"bitrate", required_argument, NULL, 'b'},
    {"gop", required_argument, NULL, 'g'},
    {"device_name", required_argument, NULL, 'd'},
    {"width", required_argument, NULL, 'w'},
    {"height", required_argument, NULL, 'h'},
    {"input_bmp_name", required_argument, NULL, 'i'},
    {"loop_count", required_argument, NULL, 'l'},
    {"output_path", required_argument, NULL, 'o'},
    {"encode", required_argument, NULL, 'e'},
    {"disp_devid", required_argument, NULL, 'D'},
    {"camid", required_argument, NULL, 'I'},
    {"video-source", required_argument, NULL, 'S'},
    {"multictx", required_argument, NULL, 'M'},
    {"fps", required_argument, NULL, 'F'},
    {"hdr_mode", required_argument, NULL, 'h' + 'm'},
    {"help", optional_argument, NULL, '?'},
    {NULL, 0, NULL, 0},
};

/******************************************************************************
 * function : show usage
 ******************************************************************************/
static void print_usage(const RK_CHAR *name) {
	printf("usage example:\n");
	printf("\t%s -w 1920 -h 1080 -F 30 -a /home/iqfiles/ -I 0 -e h264cbr -b 3072 -g 60 "
	       "-D 0 -i /usr/share/image.bmp -o /data/\n",
	       name);
	printf("\trtsp://xx.xx.xx.xx/live/0, Default OPEN\n");
#ifdef RKAIQ
	printf("\t-a | --aiq: enable aiq with dirpath provided, eg:-a "
	       "/home/iqfiles/, "
	       "set dirpath empty to using path by default, without this option aiq "
	       "should run in other application\n");
	printf("\t-M | --multictx: switch of multictx in isp, set 0 to disable, set "
	       "1 to enable. Default: 0\n");
#endif
	printf("\t-d | --device_name: set pcDeviceName, eg: /dev/video0 Default "
	       "NULL\n");
	printf("\t-I | --camid: camera ctx id, Default 0\n");
	printf("\t-S | --video-source: rgb or ir, Default rgb; selects configured camera id\n");
	printf("\t     env: HUST_VIDEO_SOURCE, HUST_RGB_CAM_ID, HUST_IR_CAM_ID,\n");
	printf("\t          HUST_RGB_DEVICE, HUST_IR_DEVICE\n");
	printf("\t-w | --width: camera with, Default 1920\n");
	printf("\t-h | --height: camera height, Default 1080\n");
	printf("\t-e | --encode: encode type, Default:h264cbr, Value:h264cbr, "
	       "h264vbr, h264avbr "
	       "h265cbr, h265vbr, h265avbr, mjpegcbr, mjpegvbr\n");
	printf("\t-b | --bitrate: encode bitrate in kbps [128..200000], Default 3072\n");
	printf("\t-g | --gop: keyframe interval in frames [1..65536], Default 2 * fps\n");
	printf("\t     HUST_IR_GOP=50, HUST_IR_BITRATE=1500; TTTRACKER_TRACK_LOG=1 enables file logging\n");
	printf("\t-i | --input_bmp_name: input file path of logo.bmp, Default NULL\n");
	printf("\t-l | --loop_count: loop count, Default -1\n");
	printf("\t-o | --output_path: encode save file path, Default /data/\n");
	printf("\t-D | --disp_devid: display DevId, Default -1\n");
	printf("\t-F | --fps: disp_layer fps, Default 30\n");
}

static RK_U64 MapVpssFrame(VIDEO_FRAME_INFO_S *frame, void **pdata) {
	RK_S32 s32Ret = RK_FAILURE;
	PIC_BUF_ATTR_S stPicBufAttr;
	MB_PIC_CAL_S stMbPicCalResult;
	memset(&stPicBufAttr, 0, sizeof(stPicBufAttr));
	memset(&stMbPicCalResult, 0, sizeof(stMbPicCalResult));

	stPicBufAttr.u32Width = frame->stVFrame.u32VirWidth;
	stPicBufAttr.u32Height = frame->stVFrame.u32VirHeight;
	stPicBufAttr.enPixelFormat = frame->stVFrame.enPixelFormat;
	stPicBufAttr.enCompMode = frame->stVFrame.enCompressMode;
	s32Ret = RK_MPI_CAL_VGS_GetPicBufferSize(&stPicBufAttr, &stMbPicCalResult);
	if (s32Ret != RK_SUCCESS) {
		RK_LOGE("RK_MPI_CAL_VGS_GetPicBufferSize failed. err=0x%x", s32Ret);
		*pdata = RK_NULL;
		return 0;
	}

	RK_MPI_SYS_MmzFlushCache(frame->stVFrame.pMbBlk, RK_TRUE);

	*pdata = RK_MPI_MB_Handle2VirAddr(frame->stVFrame.pMbBlk);

	return stMbPicCalResult.u32MBSize;
}

static uint64_t MonotonicTimeUs(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000ULL + (uint64_t)ts.tv_nsec / 1000ULL;
}

typedef struct LatestFrameSlot {
	pthread_mutex_t mutex;
	pthread_cond_t cond;
	unsigned char *data;
	size_t capacity;
	size_t size;
	int width;
	int height;
	uint64_t sequence;
	uint64_t pts;
	uint64_t capture_monotonic_us;
	uint64_t overwrite_count;
	int pending;
	int stopped;
} LatestFrameSlot;

static LatestFrameSlot g_latest_frame = {
	PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER,
	NULL, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
};

static int LatestFrameInit(size_t capacity) {
	g_latest_frame.data = (unsigned char *)malloc(capacity);
	if (!g_latest_frame.data) return -1;
	g_latest_frame.capacity = capacity;
	g_latest_frame.pending = 0;
	g_latest_frame.stopped = 0;
	g_latest_frame.overwrite_count = 0;
	return 0;
}

static void LatestFrameStop(void) {
	pthread_mutex_lock(&g_latest_frame.mutex);
	g_latest_frame.stopped = 1;
	pthread_cond_broadcast(&g_latest_frame.cond);
	pthread_mutex_unlock(&g_latest_frame.mutex);
}

static void LatestFrameDestroy(void) {
	free(g_latest_frame.data);
	g_latest_frame.data = NULL;
	g_latest_frame.capacity = 0;
}

static int LatestFramePush(const void *data, size_t size, int width, int height,
	                       uint64_t sequence, uint64_t pts,
	                       uint64_t capture_monotonic_us) {
	if (!data) return -1;
	pthread_mutex_lock(&g_latest_frame.mutex);
	if (g_latest_frame.stopped || size > g_latest_frame.capacity) {
		pthread_mutex_unlock(&g_latest_frame.mutex);
		return -1;
	}
	if (g_latest_frame.pending) g_latest_frame.overwrite_count++;
	memcpy(g_latest_frame.data, data, size);
	g_latest_frame.size = size;
	g_latest_frame.width = width;
	g_latest_frame.height = height;
	g_latest_frame.sequence = sequence;
	g_latest_frame.pts = pts;
	g_latest_frame.capture_monotonic_us = capture_monotonic_us;
	g_latest_frame.pending = 1;
	pthread_cond_signal(&g_latest_frame.cond);
	pthread_mutex_unlock(&g_latest_frame.mutex);
	return 0;
}

static int LatestFramePop(unsigned char **work_data, size_t *work_capacity,
	                      size_t *size, int *width, int *height,
	                      uint64_t *sequence, uint64_t *pts,
	                      uint64_t *capture_monotonic_us) {
	pthread_mutex_lock(&g_latest_frame.mutex);
	while (!g_latest_frame.pending && !g_latest_frame.stopped)
		pthread_cond_wait(&g_latest_frame.cond, &g_latest_frame.mutex);
	if (g_latest_frame.stopped) {
		pthread_mutex_unlock(&g_latest_frame.mutex);
		return 0;
	}
	unsigned char *tmp_data = *work_data;
	size_t tmp_capacity = *work_capacity;
	*work_data = g_latest_frame.data;
	*work_capacity = g_latest_frame.capacity;
	g_latest_frame.data = tmp_data;
	g_latest_frame.capacity = tmp_capacity;
	*size = g_latest_frame.size;
	*width = g_latest_frame.width;
	*height = g_latest_frame.height;
	*sequence = g_latest_frame.sequence;
	*pts = g_latest_frame.pts;
	*capture_monotonic_us = g_latest_frame.capture_monotonic_us;
	g_latest_frame.pending = 0;
	pthread_mutex_unlock(&g_latest_frame.mutex);
	return 1;
}

static void *AlgorithmCaptureThread(void *arg) {
	SAMPLE_MPI_CTX_S *ctx = (SAMPLE_MPI_CTX_S *)arg;
	uint64_t sequence = 0;
	uint64_t frames = 0;
	uint64_t copy_us = 0;
	uint64_t push_failures = 0;
	uint64_t last_overwrite_count = 0;
	uint64_t window_start_us = MonotonicTimeUs();
	prctl(PR_SET_NAME, "algo_capture", 0, 0, 0);

	while (!quit) {
		VIDEO_FRAME_INFO_S frame;
		memset(&frame, 0, sizeof(frame));
		RK_S32 ret = RK_MPI_VPSS_GetChnFrame(ctx->vpss.s32GrpId, 1,
		                                         &frame, 200);
		if (ret != RK_SUCCESS) continue;
		void *data = RK_NULL;
		const RK_U64 size = MapVpssFrame(&frame, &data);
		const uint64_t capture_us = MonotonicTimeUs();
		const uint64_t copy_start_us = capture_us;
		if (data && size > 0) {
			if (LatestFramePush(data, (size_t)size,
			                    (int)frame.stVFrame.u32VirWidth,
			                    (int)frame.stVFrame.u32VirHeight,
			                    ++sequence, frame.stVFrame.u64PTS,
			                    capture_us) != 0) {
				push_failures++;
			}
		}
		copy_us += MonotonicTimeUs() - copy_start_us;
		frames++;
		RK_MPI_VPSS_ReleaseChnFrame(ctx->vpss.s32GrpId, 1, &frame);

		const uint64_t now_us = MonotonicTimeUs();
		if (now_us - window_start_us >= 1000000ULL) {
			pthread_mutex_lock(&g_latest_frame.mutex);
			const uint64_t overwritten = g_latest_frame.overwrite_count;
			pthread_mutex_unlock(&g_latest_frame.mutex);
			printf("[PIPE][CAP] fps=%.1f copy=%.2fms overwritten=%llu "
			       "push_fail=%llu seq=%llu\n",
			       frames * 1000000.0 / (double)(now_us - window_start_us),
			       frames ? copy_us / (double)frames / 1000.0 : 0.0,
			       (unsigned long long)(overwritten - last_overwrite_count),
			       (unsigned long long)push_failures,
			       (unsigned long long)sequence);
			last_overwrite_count = overwritten;
			frames = 0;
			copy_us = 0;
			push_failures = 0;
			window_start_us = now_us;
		}
	}
	return NULL;
}

static void *AlgorithmWorkerThread(void *arg) {
	(void)arg;
	unsigned char *work_data = (unsigned char *)malloc(g_latest_frame.capacity);
	size_t work_capacity = g_latest_frame.capacity;
	if (!work_data) return NULL;
	prctl(PR_SET_NAME, "tracker_algo", 0, 0, 0);

	while (!quit) {
		size_t size = 0;
		int width = 0;
		int height = 0;
		uint64_t sequence = 0;
		uint64_t pts = 0;
		uint64_t capture_us = 0;
		if (!LatestFramePop(&work_data, &work_capacity, &size,
		                    &width, &height, &sequence, &pts, &capture_us))
			break;
		if (size > 0)
			processRGBData(work_data, width, height, sequence, pts, capture_us);
	}
	free(work_data);
	return NULL;
}

static void *VideoFrameThread(void *arg) {
	SAMPLE_MPI_CTX_S *ctx = (SAMPLE_MPI_CTX_S *)arg;
	LowStream *low = NULL;
	int low_initialized = 0;
	uint64_t frames = 0;
	uint64_t draw_us = 0;
	uint64_t venc_us = 0;
	uint64_t age_us = 0;
	uint64_t age_samples = 0;
	uint64_t venc_drop_count = 0;
	uint64_t overlay_frames = 0;
	uint64_t overlay_missing = 0;
	uint64_t cache_flush_failures = 0;
	uint64_t overlay_operations = 0;
	uint64_t pts_delta_us = 0, pts_samples = 0, predicted_boxes = 0, stale_results = 0;
	int last_overlay_status = 0;
	uint64_t window_start_us = MonotonicTimeUs();
	prctl(PR_SET_NAME, "video_overlay", 0, 0, 0);

	while (!quit) {
		VIDEO_FRAME_INFO_S frame;
		memset(&frame, 0, sizeof(frame));
		RK_S32 ret = RK_MPI_VPSS_GetChnFrame(ctx->vpss.s32GrpId, 0,
		                                         &frame, 200);
		if (ret != RK_SUCCESS) continue;
		void *data = RK_NULL;
		MapVpssFrame(&frame, &data);

		const uint64_t draw_start_us = MonotonicTimeUs();
		uint64_t current_age_us = 0;
		int current_operations = 0;
		int current_status = 0;
		AlgoOverlayTiming timing = {0};
		const int supported_format =
			frame.stVFrame.enPixelFormat == RK_FMT_RGB888
			|| frame.stVFrame.enPixelFormat == RK_FMT_BGR888;
		const int frame_is_rgb = frame.stVFrame.enPixelFormat == RK_FMT_RGB888;
		if (data && supported_format
			&& drawLatestAlgoResult((unsigned char *)data,
			                            (int)frame.stVFrame.u32Width,
			                            (int)frame.stVFrame.u32Height,
			                            (int)frame.stVFrame.u32VirWidth,
			                            (int)frame.stVFrame.u32VirHeight,
			                            frame_is_rgb,
			                            frame.stVFrame.u64PTS,
			                            draw_start_us, &current_age_us,
			                            &current_operations, &current_status, &timing)) {
			age_us += current_age_us;
			age_samples++;
			overlay_frames++;
			overlay_operations += (uint64_t)current_operations;
			last_overlay_status = current_status;
		} else {
			overlay_missing++;
		}
		if (timing.pts_valid) { pts_delta_us += timing.pts_delta_us; pts_samples++; }
		predicted_boxes += timing.predicted_boxes;
		stale_results += timing.stale;
		draw_us += MonotonicTimeUs() - draw_start_us;
		if (RK_MPI_SYS_MmzFlushCache(frame.stVFrame.pMbBlk, RK_FALSE)
			!= RK_SUCCESS) {
			cache_flush_failures++;
		}

		const uint64_t venc_start_us = MonotonicTimeUs();
		ret = RK_MPI_VENC_SendFrame(ctx->venc.s32ChnId, &frame, 20);
		venc_us += MonotonicTimeUs() - venc_start_us;
		if (ret != RK_SUCCESS) venc_drop_count++;
		if (data && supported_format) {
			if (!low_initialized) {
				low = low_stream_create_rgb("192.168.144.1", 5612,
				    frame.stVFrame.u32Width, frame.stVFrame.u32Height, !frame_is_rgb);
				low_initialized = 1;
			}
			low_stream_offer(low, data, frame.stVFrame.u32VirWidth, frame.stVFrame.u32VirHeight);
		}
		RK_MPI_VPSS_ReleaseChnFrame(ctx->vpss.s32GrpId, 0, &frame);
		frames++;

		const uint64_t now_us = MonotonicTimeUs();
		if (now_us - window_start_us >= 1000000ULL) {
			printf("[PIPE][VIDEO] fps=%.1f draw=%.2fms venc_wait=%.2fms "
			       "algo_age=%.1fms venc_drop=%llu overlay=%llu missing=%llu "
			       "ops=%.1f state=%d cache_fail=%llu fmt=%d size=%ux%u vir=%ux%u "
			       "pts_delta=%.1fms pts_samples=%llu predicted=%llu stale=%llu\n",
			       frames * 1000000.0 / (double)(now_us - window_start_us),
			       frames ? draw_us / (double)frames / 1000.0 : 0.0,
			       frames ? venc_us / (double)frames / 1000.0 : 0.0,
			       age_samples ? age_us / (double)age_samples / 1000.0 : 0.0,
			       (unsigned long long)venc_drop_count,
			       (unsigned long long)overlay_frames,
			       (unsigned long long)overlay_missing,
			       overlay_frames ? overlay_operations / (double)overlay_frames : 0.0,
			       last_overlay_status,
			       (unsigned long long)cache_flush_failures,
			       (int)frame.stVFrame.enPixelFormat,
			       frame.stVFrame.u32Width, frame.stVFrame.u32Height,
			       frame.stVFrame.u32VirWidth, frame.stVFrame.u32VirHeight,
			       pts_samples ? pts_delta_us / (double)pts_samples / 1000.0 : 0.0,
			       (unsigned long long)pts_samples,
			       (unsigned long long)predicted_boxes,
			       (unsigned long long)stale_results);
			frames = draw_us = venc_us = age_us = age_samples = 0;
			venc_drop_count = 0;
			overlay_frames = overlay_missing = cache_flush_failures = 0;
			overlay_operations = 0;
			pts_delta_us = pts_samples = predicted_boxes = stale_results = 0;
			window_start_us = now_us;
		}
	}
	low_stream_destroy(low);
	return NULL;
}

/******************************************************************************
 * function : venc thread
 ******************************************************************************/
static void *venc_get_stream(void *pArgs) {
	SAMPLE_VENC_CTX_S *ctx = (SAMPLE_VENC_CTX_S *)(pArgs);
	RK_S32 s32Ret = RK_FAILURE;
	char name[256] = {0};
	FILE *fp = RK_NULL;
	void *pData = RK_NULL;
	RK_S32 loopCount = 0;
	uint64_t frames = 0;
	uint64_t bytes = 0;
	uint64_t get_wait_us = 0;
	uint64_t sink_us = 0;
	uint64_t get_timeouts = 0;
	uint64_t window_start_us = MonotonicTimeUs();
	stream_metrics metrics = {0};
	metrics.start = window_start_us;

	if (ctx->dstFilePath) {
		snprintf(name, sizeof(name), "/%s/venc_%d.bin", ctx->dstFilePath, ctx->s32ChnId);
		fp = fopen(name, "wb");
		if (fp == RK_NULL) {
			printf("chn %d can't open %s file !\n", ctx->s32ChnId, ctx->dstFilePath);
			quit = true;
			return RK_NULL;
		}
	}

	while (!quit) {
		const uint64_t get_start_us = MonotonicTimeUs();
		s32Ret = RK_MPI_VENC_GetStream(ctx->s32ChnId, &ctx->stFrame, 200);
		get_wait_us += MonotonicTimeUs() - get_start_us;
		if (s32Ret == RK_SUCCESS) {
			pData = RK_MPI_MB_Handle2VirAddr(ctx->stFrame.pstPack->pMbBlk);
			// exit when complete
			if (ctx->s32loopCount > 0) {
				if (loopCount >= ctx->s32loopCount) {
					SAMPLE_COMM_VENC_ReleaseStream(ctx);
					quit = true;
					break;
				}
			}

			const uint64_t sink_start_us = MonotonicTimeUs();
			if (fp) {
				fwrite(pData, 1, ctx->stFrame.pstPack->u32Len, fp);
			}

			const uint64_t lock_start = MonotonicTimeUs();
			pthread_mutex_lock(&g_rtsp_mutex);
			const uint64_t send_start = MonotonicTimeUs();
			int tx_rc = rtsp_tx_video(g_rtsp_session, pData, ctx->stFrame.pstPack->u32Len,
			                         ctx->stFrame.pstPack->u64PTS);
			int event_rc = rtsp_do_event(g_rtsplive);
			pthread_mutex_unlock(&g_rtsp_mutex);
			const uint64_t send_end = MonotonicTimeUs();
			stream_metrics_add(&metrics, send_end, ctx->stFrame.pstPack->u32Len,
			                   send_start - lock_start, send_end - send_start,
			                   tx_rc < 0 || event_rc < 0);
			sink_us += MonotonicTimeUs() - sink_start_us;
			bytes += ctx->stFrame.pstPack->u32Len;
			frames++;

			SAMPLE_COMM_VENC_ReleaseStream(ctx);
			loopCount++;
		} else {
			get_timeouts++;
			/* Also avoid spinning on immediate encoder errors. */
			usleep(5000);
		}

		const uint64_t now_us = MonotonicTimeUs();
		if (now_us - window_start_us >= 1000000ULL) {
			const uint64_t get_attempts = frames + get_timeouts;
			printf("[PIPE][STREAM] fps=%.1f get_wait=%.2fms sink=%.2fms "
			       "bitrate=%.2fMbps get_timeout=%llu\n",
			       frames * 1000000.0 / (double)(now_us - window_start_us),
			       get_attempts ? get_wait_us / (double)get_attempts / 1000.0 : 0.0,
			       frames ? sink_us / (double)frames / 1000.0 : 0.0,
			       bytes * 8.0 / (double)(now_us - window_start_us),
			       (unsigned long long)get_timeouts);
			frames = bytes = get_wait_us = sink_us = get_timeouts = 0;
			window_start_us = now_us;
		}
		stream_metrics_report(&metrics, "RGB", now_us);
		/* GetStream already waits for the next encoder output; no extra 1ms sleep. */
	}

	if (fp)
		fclose(fp);

	return RK_NULL;
}

/******************************************************************************
 * function    : main()
 * Description : main
 ******************************************************************************/
int main(int argc, char *argv[]) {
	SAMPLE_MPI_CTX_S *ctx;
	int video_width = 1920;
	int video_height = 1080;
	int venc_width = video_width;
	int venc_height = video_height;
	int disp_width = video_width;
	int disp_height = video_height;
	int disp_fps = 30;
	RK_CHAR *pDeviceName = NULL;
	RK_CHAR *pInPathBmp = NULL;
	RK_CHAR *pOutPathVenc = NULL;
	CODEC_TYPE_E enCodecType = RK_CODEC_TYPE_H264;
	VENC_RC_MODE_E enRcMode = VENC_RC_MODE_H264CBR;
	RK_CHAR *pCodecName = "H264";
	RK_S32 s32CamId = 0;
	RK_S32 rgbCamId = 0;
	RK_S32 irCamId = 1;
	HUST_VIDEO_SOURCE_E videoSource = HUST_VIDEO_SOURCE_RGB;
	int camIdExplicit = 0;
	int deviceNameExplicit = 0;
	RK_S32 s32DisId = -1;
	RK_S32 s32DisLayerId = 0;
	RK_S32 s32loopCnt = -1;
	RK_S32 s32BitRate = 3072;
	int gop = 0;
	MPP_CHN_S stSrcChn, stDestChn;

	if (argc < 2) {
		print_usage(argv[0]);
		return 0;
	}

	ctx = (SAMPLE_MPI_CTX_S *)(malloc(sizeof(SAMPLE_MPI_CTX_S)));
	memset(ctx, 0, sizeof(SAMPLE_MPI_CTX_S));

	signal(SIGINT, sigterm_handler);
	signal(SIGTERM, sigterm_handler);
	signal(SIGPIPE, SIG_IGN);

#ifdef RKAIQ
	RK_BOOL bMultictx = RK_FALSE;
#endif
	const char *sourceEnv = getenv("HUST_VIDEO_SOURCE");
	const char *rgbCamEnv = getenv("HUST_RGB_CAM_ID");
	const char *irCamEnv = getenv("HUST_IR_CAM_ID");
	const char *rgbDeviceEnv = getenv("HUST_RGB_DEVICE");
	const char *irDeviceEnv = getenv("HUST_IR_DEVICE");
	if (sourceEnv && hust_video_source_parse(sourceEnv, &videoSource) != 0) {
		fprintf(stderr, "Invalid HUST_VIDEO_SOURCE='%s' (expected rgb or ir)\n",
		        sourceEnv);
		return 2;
	}
	if (rgbCamEnv && hust_video_source_parse_cam_id(rgbCamEnv, &rgbCamId) != 0) {
		fprintf(stderr, "Invalid HUST_RGB_CAM_ID='%s'\n", rgbCamEnv);
		return 2;
	}
	if (irCamEnv && hust_video_source_parse_cam_id(irCamEnv, &irCamId) != 0) {
		fprintf(stderr, "Invalid HUST_IR_CAM_ID='%s'\n", irCamEnv);
		return 2;
	}
	int c;
	char *iq_file_dir = NULL;
	while ((c = getopt_long(argc, argv, optstr, long_options, NULL)) != -1) {
		const char *tmp_optarg = optarg;
		switch (c) {
		case 'a':
			if (!optarg && NULL != argv[optind] && '-' != argv[optind][0]) {
				tmp_optarg = argv[optind++];
			}
			if (tmp_optarg) {
				iq_file_dir = (char *)tmp_optarg;
			} else {
				iq_file_dir = NULL;
			}
			break;
		case 'b':
			if (stream_parse_int(optarg, 128, 200000, &s32BitRate)) {
				fprintf(stderr, "Invalid bitrate: expected 128..200000 kbps\n"); free(ctx); return 2;
			}
			break;
		case 'g':
			if (stream_parse_int(optarg, 1, 65536, &gop)) {
				fprintf(stderr, "Invalid GOP: expected 1..65536 frames\n"); free(ctx); return 2;
			}
			break;
		case 'd':
			pDeviceName = optarg;
			deviceNameExplicit = 1;
			break;
		case 'D':
			s32DisId = atoi(optarg);
			break;
		case 'e':
			if (!strcmp(optarg, "h264cbr")) {
				enCodecType = RK_CODEC_TYPE_H264;
				enRcMode = VENC_RC_MODE_H264CBR;
				pCodecName = "H264";
			} else if (!strcmp(optarg, "h264vbr")) {
				enCodecType = RK_CODEC_TYPE_H264;
				enRcMode = VENC_RC_MODE_H264VBR;
				pCodecName = "H264";
			} else if (!strcmp(optarg, "h264avbr")) {
				enCodecType = RK_CODEC_TYPE_H264;
				enRcMode = VENC_RC_MODE_H264AVBR;
				pCodecName = "H264";
			} else if (!strcmp(optarg, "h265cbr")) {
				enCodecType = RK_CODEC_TYPE_H265;
				enRcMode = VENC_RC_MODE_H265CBR;
				pCodecName = "H265";
			} else if (!strcmp(optarg, "h265vbr")) {
				enCodecType = RK_CODEC_TYPE_H265;
				enRcMode = VENC_RC_MODE_H265VBR;
				pCodecName = "H265";
			} else if (!strcmp(optarg, "h265avbr")) {
				enCodecType = RK_CODEC_TYPE_H265;
				enRcMode = VENC_RC_MODE_H265AVBR;
				pCodecName = "H265";
			} else if (!strcmp(optarg, "mjpegcbr")) {
				enCodecType = RK_CODEC_TYPE_MJPEG;
				enRcMode = VENC_RC_MODE_MJPEGCBR;
				pCodecName = "MJPEG";
			} else if (!strcmp(optarg, "mjpegvbr")) {
				enCodecType = RK_CODEC_TYPE_MJPEG;
				enRcMode = VENC_RC_MODE_MJPEGVBR;
				pCodecName = "MJPEG";
			} else {
				printf("ERROR: Invalid encoder type.\n");
				return 0;
			}
			break;
		case 'w':
			video_width = atoi(optarg);
			venc_width = video_width;
			disp_width = video_width;
			break;
		case 'h':
			video_height = atoi(optarg);
			venc_height = video_height;
			disp_height = video_height;
			break;
		case 'F':
			if (stream_parse_int(optarg, 1, 120, &disp_fps)) {
				fprintf(stderr, "Invalid fps: expected 1..120\n"); free(ctx); return 2;
			}
			break;
		case 'I':
			s32CamId = atoi(optarg);
			camIdExplicit = 1;
			break;
		case 'S':
			if (hust_video_source_parse(optarg, &videoSource) != 0) {
				fprintf(stderr, "Invalid --video-source '%s' (expected rgb or ir)\n",
				        optarg);
				return 2;
			}
			break;
		case 'i':
			pInPathBmp = optarg;
			break;
		case 'l':
			s32loopCnt = atoi(optarg);
			break;
		case 'L':
			s32DisLayerId = atoi(optarg);
			break;
		case 'o':
			pOutPathVenc = optarg;
			break;
#ifdef RKAIQ
		case 'M':
			if (atoi(optarg)) {
				bMultictx = RK_TRUE;
			}
			break;
#endif
		case '?':
		default:
			print_usage(argv[0]);
			return 0;
		}
	}
	if (!camIdExplicit)
		s32CamId = hust_video_source_cam_id(videoSource, rgbCamId, irCamId);
	if (!deviceNameExplicit)
		pDeviceName = (RK_CHAR *)(videoSource == HUST_VIDEO_SOURCE_IR
		                           ? irDeviceEnv : rgbDeviceEnv);

	printf("#VideoSource: %s\n", hust_video_source_name(videoSource));
	printf("#CameraMap: rgb=%d ir=%d%s\n", rgbCamId, irCamId,
	       camIdExplicit ? " (camid overridden by CLI)" : "");
	printf("#CameraIdx: %d\n", s32CamId);
	printf("#pDeviceName: %s\n", pDeviceName);
	printf("#CodecName:%s\n", pCodecName);
	printf("#Output Path: %s\n", pOutPathVenc);
	printf("#IQ Path: %s\n", iq_file_dir);
	if (iq_file_dir) {
#ifdef RKAIQ
		printf("#Rkaiq XML DirPath: %s\n", iq_file_dir);
		printf("#bMultictx: %d\n\n", bMultictx);
		rk_aiq_working_mode_t hdr_mode = RK_AIQ_WORKING_MODE_NORMAL;

		SAMPLE_COMM_ISP_Init(s32CamId, hdr_mode, bMultictx, iq_file_dir);
		SAMPLE_COMM_ISP_Run(s32CamId);
#endif
	}

	// init rtsp
	g_rtsplive = create_rtsp_demo(554);
	g_rtsp_session = rtsp_new_session(g_rtsplive, "/live/0");
	if (enCodecType == RK_CODEC_TYPE_H264) {
		rtsp_set_video(g_rtsp_session, RTSP_CODEC_ID_VIDEO_H264, NULL, 0);
	} else if (enCodecType == RK_CODEC_TYPE_H265) {
		rtsp_set_video(g_rtsp_session, RTSP_CODEC_ID_VIDEO_H265, NULL, 0);
	} else {
		printf("not support other type\n");
		return -1;
	}
	rtsp_sync_video_ts(g_rtsp_session, rtsp_get_reltime(), rtsp_get_ntptime());

	// Init servo communication
	if (servo_proto_init(NULL) != 0) {
		printf("servo_proto_init failed, continuing without servo\n");
		g_servo_ok = 0;
	} else {
		servo_proto_register_cb(on_servo_rx, NULL);
		servo_get_version();
		usleep(100000); /* 100ms: 等待版本查询完成 */

		/* 确保云台不在锁定模式 */
		servo_set_lock_mode(SERVO_LOCK_MODE_EXIT);
		usleep(50000);
		/* 最大拨轮速度拉满 (范围 5~100) */
		servo_set_max_wheel_speed(100, 100);
		usleep(50000);
		printf("Servo init OK: lock=EXIT, wheel_speed=100\n");

		g_servo_ok = 1;
	}
	if (tracker_bridge_start() != 0) {
		printf("tracker bridge async servo thread start failed\n");
	}

	// Start TCP command server
	if (tcp_cmd_server_start(on_tcp_command) != 0) {
		printf("TCP command server start failed\n");
	}

	if (RK_MPI_SYS_Init() != RK_SUCCESS) {
		goto __FAILED;
	}

	if (mino_ir_start(g_rtsplive, &g_rtsp_mutex) != 0)
		fprintf(stderr, "[IR] initialization failed; RGB continues\n");

	// Init VI[0]
	ctx->vi.u32Width = video_width;
	ctx->vi.u32Height = video_height;
	ctx->vi.s32DevId = s32CamId;
	ctx->vi.u32PipeId = ctx->vi.s32DevId;
	ctx->vi.s32ChnId = 1;
	ctx->vi.stChnAttr.stIspOpt.u32BufCount = 3;
	ctx->vi.stChnAttr.stIspOpt.enMemoryType = VI_V4L2_MEMORY_TYPE_DMABUF;
	ctx->vi.stChnAttr.u32Depth = 0;
	ctx->vi.stChnAttr.enPixelFormat = RK_FMT_YUV420SP;
	ctx->vi.stChnAttr.stFrameRate.s32SrcFrameRate = -1;
	ctx->vi.stChnAttr.stFrameRate.s32DstFrameRate = -1;
	const char *capture_fps_env=getenv("HUST_RGB_SOURCE_FPS");
	int capture_fps=0;
	if(capture_fps_env && !stream_parse_int(capture_fps_env,1,240,&capture_fps) && capture_fps>=disp_fps) {
		ctx->vi.stChnAttr.stFrameRate.s32SrcFrameRate=capture_fps;
		ctx->vi.stChnAttr.stFrameRate.s32DstFrameRate=disp_fps;
		fprintf(stderr,"[CAPTURE] VI rate control %d -> %d fps\n",capture_fps,disp_fps);
	}
	if (pDeviceName) {
		strcpy(ctx->vi.stChnAttr.stIspOpt.aEntityName, pDeviceName);
	}
	SAMPLE_COMM_VI_CreateChn(&ctx->vi);

	// Init VPSS[0]
	ctx->vpss.s32GrpId = 0;
	ctx->vpss.s32ChnId = 0;
	// RGA_device: VIDEO_PROC_DEV_RGA GPU_device: VIDEO_PROC_DEV_GPU
	ctx->vpss.enVProcDevType = VIDEO_PROC_DEV_RGA;
	ctx->vpss.stGrpVpssAttr.enPixelFormat = RK_FMT_YUV420SP;
	ctx->vpss.stGrpVpssAttr.enCompressMode = COMPRESS_MODE_NONE; // no compress

	ctx->vpss.stCropInfo.bEnable = RK_FALSE;
	ctx->vpss.stCropInfo.enCropCoordinate = VPSS_CROP_RATIO_COOR;
	ctx->vpss.stCropInfo.stCropRect.s32X = 0;
	ctx->vpss.stCropInfo.stCropRect.s32Y = 0;
	ctx->vpss.stCropInfo.stCropRect.u32Width = video_width;
	ctx->vpss.stCropInfo.stCropRect.u32Height = video_height;

	ctx->vpss.stChnCropInfo[0].bEnable = RK_TRUE;
	ctx->vpss.stChnCropInfo[0].enCropCoordinate = VPSS_CROP_RATIO_COOR;
	ctx->vpss.stChnCropInfo[0].stCropRect.s32X = 0;
	ctx->vpss.stChnCropInfo[0].stCropRect.s32Y = 0;
	ctx->vpss.stChnCropInfo[0].stCropRect.u32Width = venc_width * 1000 / video_width;
	ctx->vpss.stChnCropInfo[0].stCropRect.u32Height = venc_height * 1000 / video_height;
	ctx->vpss.s32ChnRotation[0] = ROTATION_180; // ROTATION_90
	ctx->vpss.stRotationEx[0].bEnable = RK_FALSE;
	ctx->vpss.stRotationEx[0].stRotationEx.u32Angle = 60;
	ctx->vpss.stVpssChnAttr[0].enChnMode = VPSS_CHN_MODE_USER;
	ctx->vpss.stVpssChnAttr[0].enCompressMode = COMPRESS_MODE_NONE;
	ctx->vpss.stVpssChnAttr[0].enDynamicRange = DYNAMIC_RANGE_SDR8;
	ctx->vpss.stVpssChnAttr[0].enPixelFormat = RK_FMT_RGB888;
	ctx->vpss.stVpssChnAttr[0].stFrameRate.s32SrcFrameRate = -1;
	ctx->vpss.stVpssChnAttr[0].stFrameRate.s32DstFrameRate = -1;
	ctx->vpss.stVpssChnAttr[0].u32Width = venc_width;
	ctx->vpss.stVpssChnAttr[0].u32Height = venc_height;
	ctx->vpss.stVpssChnAttr[0].u32Depth = 2;

	// ch0保持视频输出，ch1专供算法采集；算法通道深度1，旧帧由应用层覆盖。
	ctx->vpss.stChnCropInfo[1] = ctx->vpss.stChnCropInfo[0];
	ctx->vpss.s32ChnRotation[1] = ctx->vpss.s32ChnRotation[0];
	ctx->vpss.stRotationEx[1] = ctx->vpss.stRotationEx[0];
	ctx->vpss.stVpssChnAttr[1] = ctx->vpss.stVpssChnAttr[0];
	ctx->vpss.stVpssChnAttr[1].u32Depth = 1;

	// if (s32DisId >= 0) {
	// 	ctx->vpss.s32ChnRotation[1] = ROTATION_0;
	// 	ctx->vpss.stVpssChnAttr[1].enChnMode = VPSS_CHN_MODE_USER;
	// 	ctx->vpss.stVpssChnAttr[1].enCompressMode = COMPRESS_MODE_NONE;
	// 	ctx->vpss.stVpssChnAttr[1].enDynamicRange = DYNAMIC_RANGE_SDR8;
	// 	ctx->vpss.stVpssChnAttr[1].enPixelFormat = RK_FMT_YUV420SP;
	// 	ctx->vpss.stVpssChnAttr[1].stFrameRate.s32SrcFrameRate = -1;
	// 	ctx->vpss.stVpssChnAttr[1].stFrameRate.s32DstFrameRate = -1;
	// 	ctx->vpss.stVpssChnAttr[1].u32Width = video_width;
	// 	ctx->vpss.stVpssChnAttr[1].u32Height = video_height;
	// }
	SAMPLE_COMM_VPSS_CreateChn(&ctx->vpss);

	// Init VENC[0]
	ctx->venc.s32ChnId = 0;
	ctx->venc.u32Width = venc_width;
	ctx->venc.u32Height = venc_height;
	ctx->venc.u32Fps = disp_fps;
	ctx->venc.u32Gop = gop ? gop : 2 * disp_fps;
	printf("[STREAM][CONFIG] codec=%s rc=%d fps=%d gop=%u bitrate=%dkbps\n",
	       pCodecName, enRcMode, disp_fps, ctx->venc.u32Gop, s32BitRate);
	ctx->venc.u32BitRate = s32BitRate;
	ctx->venc.enCodecType = enCodecType;
	ctx->venc.enRcMode = enRcMode;
	ctx->venc.u32StreamBufCnt = 3;
	ctx->venc.getStreamCbFunc = venc_get_stream;
	ctx->venc.s32loopCount = s32loopCnt;
	ctx->venc.dstFilePath = pOutPathVenc;
	// H264  66：Baseline  77：Main Profile 100：High Profile
	// H265  0：Main Profile  1：Main 10 Profile
	// MJPEG 0：Baseline
	ctx->venc.stChnAttr.stVencAttr.u32Profile = 100;
	ctx->venc.stChnAttr.stGopAttr.enGopMode = VENC_GOPMODE_NORMALP; // VENC_GOPMODE_SMARTP
	SAMPLE_COMM_VENC_CreateChn(&ctx->venc);

	// if (s32DisId >= 0) {
	// 	// Init VO[0]
	// 	ctx->vo.s32DevId = 0;
	// 	ctx->vo.s32ChnId = 0;
	// 	ctx->vo.s32LayerId = s32DisLayerId;
	// 	ctx->vo.Volayer_mode = VO_LAYER_MODE_GRAPHIC;
	// 	ctx->vo.u32DispBufLen = 3;
	// 	ctx->vo.stVoPubAttr.enIntfType = VO_INTF_HDMI;
	// 	ctx->vo.stVoPubAttr.enIntfSync = VO_OUTPUT_1080P60;
	// 	ctx->vo.stLayerAttr.stDispRect.s32X = 0;
	// 	ctx->vo.stLayerAttr.stDispRect.s32Y = 0;
	// 	ctx->vo.stLayerAttr.stDispRect.u32Width = disp_width;
	// 	ctx->vo.stLayerAttr.stDispRect.u32Height = disp_height;
	// 	ctx->vo.stLayerAttr.stImageSize.u32Width = disp_width;
	// 	ctx->vo.stLayerAttr.stImageSize.u32Height = disp_height;
	// 	ctx->vo.stLayerAttr.u32DispFrmRt = disp_fps;
	// 	ctx->vo.stLayerAttr.enPixFormat = RK_FMT_RGB888;
	// 	// ctx->vo.stLayerAttr.bDoubleFrame = RK_FALSE;
	// 	ctx->vo.stChnAttr.stRect.s32X = 0;
	// 	ctx->vo.stChnAttr.stRect.s32Y = 0;
	// 	ctx->vo.stChnAttr.stRect.u32Width = disp_width;
	// 	ctx->vo.stChnAttr.stRect.u32Height = disp_height;
	// 	ctx->vo.stChnAttr.u32Priority = 1;
	// 	SAMPLE_COMM_VO_CreateChn(&ctx->vo);
	// }

	// Bind VI[0] and VPSS[0]
	stSrcChn.enModId = RK_ID_VI;
	stSrcChn.s32DevId = ctx->vi.s32DevId;
	stSrcChn.s32ChnId = ctx->vi.s32ChnId;
	stDestChn.enModId = RK_ID_VPSS;
	stDestChn.s32DevId = ctx->vpss.s32GrpId;
	stDestChn.s32ChnId = ctx->vpss.s32ChnId;
	SAMPLE_COMM_Bind(&stSrcChn, &stDestChn);

	// // Bind VPSS[0] and VENC[0]
	// stSrcChn.enModId = RK_ID_VPSS;
	// stSrcChn.s32DevId = ctx->vpss.s32GrpId;
	// stSrcChn.s32ChnId = ctx->vpss.s32ChnId;
	// stDestChn.enModId = RK_ID_VENC;
	// stDestChn.s32DevId = 0;
	// stDestChn.s32ChnId = ctx->venc.s32ChnId;
	// SAMPLE_COMM_Bind(&stSrcChn, &stDestChn);

	pthread_t video_thread;
	pthread_t capture_thread;
	pthread_t algorithm_thread;
	int video_thread_started = 0;
	int capture_thread_started = 0;
	int algorithm_thread_started = 0;
	const size_t frame_capacity =
		(size_t)((venc_width + 15) & ~15) * (size_t)((venc_height + 15) & ~15) * 3U;
	if (LatestFrameInit(frame_capacity) != 0) {
		RK_LOGE("LatestFrameInit failed, capacity=%zu", frame_capacity);
		quit = 1;
	} else {
		if (pthread_create(&algorithm_thread, NULL, AlgorithmWorkerThread, ctx) == 0)
			algorithm_thread_started = 1;
		else
			quit = 1;
		if (!quit && pthread_create(&capture_thread, NULL, AlgorithmCaptureThread, ctx) == 0)
			capture_thread_started = 1;
		else if (!quit)
			quit = 1;
		if (!quit && pthread_create(&video_thread, NULL, VideoFrameThread, ctx) == 0)
			video_thread_started = 1;
		else if (!quit)
			quit = 1;
	}

	// if (s32DisId >= 0) {
	// 	// Bind VPSS[1] and VO[0]
	// 	stSrcChn.enModId = RK_ID_VPSS;
	// 	stSrcChn.s32DevId = ctx->vpss.s32GrpId;
	// 	stSrcChn.s32ChnId = 1;
	// 	stDestChn.enModId = RK_ID_VO;
	// 	stDestChn.s32DevId = ctx->vo.s32LayerId;
	// 	stDestChn.s32ChnId = ctx->vo.s32ChnId;
	// 	SAMPLE_COMM_Bind(&stSrcChn, &stDestChn);
	// }

	printf("%s initial finish\n", __func__);

	while (!quit) {
		sleep(1);
	}

	printf("%s exit!\n", __func__);
	mino_ir_stop();
	LatestFrameStop();

	if (video_thread_started)
		pthread_join(video_thread, NULL);
	if (capture_thread_started)
		pthread_join(capture_thread, NULL);
	if (algorithm_thread_started)
		pthread_join(algorithm_thread, NULL);

	if (ctx->venc.getStreamCbFunc) {
		pthread_join(ctx->venc.getStreamThread, NULL);
	}
	LatestFrameDestroy();

	if (g_rtsplive) {
		rtsp_del_demo(g_rtsplive);
		g_rtsplive = NULL;
	}

	// if (s32DisId >= 0) {
	// 	// UnBind VPSS[0] and VO[0]
	// 	stSrcChn.enModId = RK_ID_VPSS;
	// 	stSrcChn.s32DevId = ctx->vpss.s32GrpId;
	// 	stSrcChn.s32ChnId = 1;
	// 	stDestChn.enModId = RK_ID_VO;
	// 	stDestChn.s32DevId = ctx->vo.s32LayerId;
	// 	stDestChn.s32ChnId = ctx->vo.s32ChnId;
	// 	SAMPLE_COMM_UnBind(&stSrcChn, &stDestChn);
	// }

	// // UnBind VPSS[0] and VENC[0]
	// stSrcChn.enModId = RK_ID_VPSS;
	// stSrcChn.s32DevId = ctx->vpss.s32GrpId;
	// stSrcChn.s32ChnId = ctx->vpss.s32ChnId;
	// stDestChn.enModId = RK_ID_VENC;
	// stDestChn.s32DevId = 0;
	// stDestChn.s32ChnId = ctx->venc.s32ChnId;
	// SAMPLE_COMM_UnBind(&stSrcChn, &stDestChn);

	// UnBind VI[0] and VPSS[0]
	stSrcChn.enModId = RK_ID_VI;
	stSrcChn.s32DevId = ctx->vi.s32DevId;
	stSrcChn.s32ChnId = ctx->vi.s32ChnId;
	stDestChn.enModId = RK_ID_VPSS;
	stDestChn.s32DevId = ctx->vpss.s32GrpId;
	stDestChn.s32ChnId = ctx->vpss.s32ChnId;
	SAMPLE_COMM_UnBind(&stSrcChn, &stDestChn);

	if (s32DisId >= 0) {
		// Destroy VO[0]
		SAMPLE_COMM_VO_DestroyChn(&ctx->vo);
	}
	// Destroy VENC[0]
	SAMPLE_COMM_VENC_DestroyChn(&ctx->venc);
	// Destroy VPSS[0]
	SAMPLE_COMM_VPSS_DestroyChn(&ctx->vpss);
	// Destroy VI[0]
	SAMPLE_COMM_VI_DestroyChn(&ctx->vi);

__FAILED:
	mino_ir_stop();
	if (g_rtsplive) {
		rtsp_del_demo(g_rtsplive);
		g_rtsplive = NULL;
	}
	/* These services start before RK MPI and must also stop on early failure. */
	tcp_cmd_server_stop();
	tracker_bridge_stop();
	if (g_servo_ok) {
		servo_proto_deinit();
		g_servo_ok = 0;
	}
	RK_MPI_SYS_Exit();
	if (iq_file_dir) {
#ifdef RKAIQ
		SAMPLE_COMM_ISP_Stop(s32CamId);
#endif
	}
	if (ctx) {
		free(ctx);
		ctx = RK_NULL;
	}

	return 0;
}

#ifdef __cplusplus
#if __cplusplus
}
#endif
#endif /* End of #ifdef __cplusplus */
